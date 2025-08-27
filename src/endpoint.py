from flask import Flask, request, jsonify
import os, re, json, traceback, boto3
from botocore.exceptions import BotoCoreError, ClientError

# ===== Regiões separadas =====
def _env(name, default=""):
    return (os.getenv(name, default) or "").strip()

BEDROCK_REGION = _env("BEDROCK_REGION", "us-east-1")     # Bedrock costuma ficar em us-east-1
KENDRA_REGION  = _env("KENDRA_REGION", _env("AWS_REGION", "us-east-1"))  # onde está seu índice

KENDRA_INDEX_ID = _env("KENDRA_INDEX_ID")      # obrigatório p/ retrieve
MODEL_ID       = _env("MODEL_ID")              # opcional (on-demand)
MODEL_ARN      = _env("MODEL_ARN")             # opcional (provisioned/profile)

# Fallback on-demand atual (ajuste se quiser outro)
DEFAULT_ON_DEMAND = "anthropic.claude-v2:1"

def _resolve_model_identifier() -> str:
    """
    Retorna um identificador aceito pelo Bedrock:
    - Se for ARN de provisioned/profile => usa o ARN inteiro
    - Se for ARN de foundation-model => extrai o modelId
    - Senão usa MODEL_ID; se vazio, usa fallback on-demand
    """
    arn = MODEL_ARN
    if arn.startswith("arn:aws:bedrock:"):
        # Provisioned / Inference Profiles são aceitos como ARN
        if any(p in arn for p in (":provisioned-model/", ":application-inference-profile/", ":system-inference-profile/")):
            return arn
        # Foundation model ARN -> extrai modelId (ex.: .../foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0)
        if "/foundation-model/" in arn:
            return arn.split("/foundation-model/")[-1]
        # Qualquer outro ARN: melhor não usar
    return MODEL_ID or DEFAULT_ON_DEMAND

MODEL_IDENTIFIER = _resolve_model_identifier()

# ===== Clients =====
bedrock_rt = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
def kendra_client():
    return boto3.client("kendra", region_name=KENDRA_REGION)

SYSTEM_INSTRUCTIONS = (
    "Você é uma IA que responde em português, de forma objetiva, "
    "usando EXCLUSIVAMENTE o CONTEXTO.\n"
    "Se o contexto não trouxer evidências suficientes, responda exatamente: "
    "\"Não encontrei informações sobre isso na base de conhecimento.\""
)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ===== Helpers =====
def missing_env():
    miss = []
    if not KENDRA_INDEX_ID:  miss.append("KENDRA_INDEX_ID")
    # Não exigimos MODEL_ARN: usamos MODEL_ID ou fallback
    return miss

@app.after_request
def add_id_header(resp):
    resp.headers["X-Service"] = "flask-kendra"
    resp.headers["X-Bedrock-Region"] = BEDROCK_REGION
    resp.headers["X-Kendra-Region"]  = KENDRA_REGION
    resp.headers["X-Model"]          = MODEL_IDENTIFIER
    return resp

def make_query_variants(q: str):
    """Gera variações úteis para IDs: US-5270, US 5270, US5270, 5270, etc."""
    variants = [q]
    m = re.search(r'\b([A-Za-z]{1,6})[-\s]?(\d{2,8})\b', q)
    if m:
        prefix, num = m.group(1).upper(), m.group(2)
        variants += [
            f"{prefix}-{num}", f"{prefix} {num}", f"{prefix}{num}",
            f"{num}", f"user story {num}", f"user story #{num}",
            f"US-{num}", f"US {num}", f"US{num}",
        ]
    out, seen = [], set()
    for v in variants:
        if v not in seen:
            out.append(v); seen.add(v)
    return out

# ===== Retrieval (Kendra) =====
def retrieve_kendra(query: str, top_k: int = 12):
    cli = kendra_client()
    items = []
    for qv in make_query_variants(query):
        resp = cli.query(IndexId=KENDRA_INDEX_ID, QueryText=qv, PageSize=min(top_k, 50))
        for it in resp.get("ResultItems", []):
            if it.get("Type") not in ("DOCUMENT", "ANSWER", "QUESTION_ANSWER"):
                continue
            uri = it.get("DocumentURI") or ""
            excerpt = it.get("DocumentExcerpt", {}).get("Text") or ""
            if not excerpt:
                for aa in it.get("AdditionalAttributes", []):
                    if aa.get("Key") == "PassageText":
                        tw = aa.get("Value", {}).get("TextWithHighlightsValue", {})
                        if tw.get("Text"):
                            excerpt = tw["Text"]
                            break
            if not excerpt:
                continue
            items.append({"uri": uri or "(sem URI)", "excerpt": excerpt.strip(), "score": None})
        if len(items) >= top_k:
            break

    # dedup por (uri, início do texto)
    uniq, seen = [], set()
    for x in items:
        key = (x["uri"], x["excerpt"][:80])
        if key in seen: 
            continue
        seen.add(key)
        uniq.append(x)
    return uniq[:top_k]

# ===== Geração (Bedrock Runtime) =====
def generate_with_bedrock_runtime(question: str, snippets: list[str]) -> str:
    """
    Usa Bedrock Runtime para gerar resposta condicionada ao contexto (snippets).
    Suporta Claude 2 (prompt) e Claude 3 (messages).
    """
    model_id = MODEL_IDENTIFIER
    context = "\n\n---\n".join(snippets)
    if len(context) > 8000:
        context = context[:8000]

    # Claude 3 / Converse-style
    if model_id.startswith("anthropic.claude-3") or model_id.startswith("arn:aws:bedrock:"):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": (
                        f"{SYSTEM_INSTRUCTIONS}\n\n"
                        f"CONTEXTO:\n{context}\n\n"
                        f"PERGUNTA: {question}\n\n"
                        "Responda de forma concisa e ao final escreva uma seção 'Fontes' listando as URIs se houver."
                    )}
                ]}
            ],
            "max_tokens": 800,
            "temperature": 0.2
        }
        resp = bedrock_rt.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body)
        )
        payload = json.loads(resp["body"].read())
        # Tentativas de extração comuns (depende do modelo)
        return (payload.get("output_text")
                or payload.get("generation")
                or payload.get("content", [{}])[0].get("text", "")
                or "").strip()

    # Claude 2.x (prompt Human/Assistant)
    prompt = (
        f"\n\nHuman: {SYSTEM_INSTRUCTIONS}\n\n"
        f"CONTEXTO:\n{context}\n\n"
        f"PERGUNTA: {question}\n\n"
        "Responda de forma concisa e ao final escreva uma seção 'Fontes' listando as URIs se houver.\n"
        "\n\nAssistant:"
    )
    body = {
        "prompt": prompt,
        "max_tokens_to_sample": 800,
        "temperature": 0.2,
        "stop_sequences": ["\n\nHuman:"]
    }
    resp = bedrock_rt.invoke_model(
        modelId=model_id or "anthropic.claude-v2:1",
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body)
    )
    payload = json.loads(resp["body"].read())
    return (payload.get("completion") or "").strip()

# ===== Routes =====
@app.get("/ping")
def ping():
    return jsonify({
        "ok": True,
        "bedrock_region": BEDROCK_REGION,
        "kendra_region": KENDRA_REGION,
        "model": MODEL_IDENTIFIER
    })

@app.post("/retrieve")
def retrieve_only():
    try:
        data = request.get_json(force=True) or {}
        q = (data.get("q") or data.get("message") or "").strip()
        top_k = int(data.get("k") or 12)
        if not q:
            return jsonify({"error": "Informe 'q'"}), 400

        hits = retrieve_kendra(q, top_k)
        return jsonify({"hits": hits}), 200
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.post("/chat")
def chat():
    try:
        miss = missing_env()
        if miss:
            return jsonify({"error": "Missing env vars", "missing": miss}), 500

        data = request.get_json(force=True) or {}
        user_msg = (data.get("message") or "").strip()
        k = int(data.get("k") or 12)
        if not user_msg:
            return jsonify({"error": "Body precisa conter JSON com o campo 'message'."}), 400

        # 1) Retrieve no Kendra
        k_hits = retrieve_kendra(user_msg, k)
        if not k_hits:
            return jsonify({
                "answer": "Não encontrei informações sobre isso na base de conhecimento.",
                "citations": []
            }), 200

        citations = [{"uri": h["uri"], "score": h["score"]} for h in k_hits]
        snippets  = [h["excerpt"] for h in k_hits]

        # 2) Geração usando somente o contexto recuperado
        answer = generate_with_bedrock_runtime(user_msg, snippets) or \
                 "Não encontrei informações sobre isso na base de conhecimento."

        return jsonify({"answer": answer, "citations": citations}), 200

    except (BotoCoreError, ClientError) as e:
        return jsonify({"error": "AWS error", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
