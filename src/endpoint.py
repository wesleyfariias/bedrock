from flask import Flask, request, jsonify
import os, re, json, traceback, boto3
from botocore.exceptions import BotoCoreError, ClientError

# =======================
# Config / Env
# =======================
REGION = os.getenv("AWS_REGION", "us-east-1")
KENDRA_INDEX_ID = os.getenv("KENDRA_INDEX_ID")     # obrigatório p/ usar Kendra
MODEL_ARN = os.getenv("MODEL_ARN")                 # ex.: arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-v2:1

# Cliente do Bedrock Runtime (geração)
bedrock_rt = boto3.client("bedrock-runtime", region_name=REGION)

SYSTEM_INSTRUCTIONS = (
    "Você é uma IA que responde em português, de forma objetiva, "
    "usando EXCLUSIVAMENTE o CONTEXTO.\n"
    "Se o contexto não trouxer evidências suficientes, responda exatamente: "
    "\"Não encontrei informações sobre isso na base de conhecimento.\""
)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# =======================
# Helpers
# =======================
def kendra_client():
    return boto3.client("kendra", region_name=REGION)

def missing_env():
    miss = []
    if not REGION:           miss.append("AWS_REGION")
    if not MODEL_ARN:        miss.append("MODEL_ARN")
    if not KENDRA_INDEX_ID:  miss.append("KENDRA_INDEX_ID")
    return miss

@app.after_request
def add_id_header(resp):
    resp.headers["X-Service"] = "flask-kendra"
    return resp

def _resolve_model_id(s: str) -> str:
    # aceita ARN completo ou o ID curto (ex.: anthropic.claude-v2:1)
    if s and s.startswith("arn:aws:bedrock:"):
        return s.split("foundation-model/")[-1]
    return s

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

# =======================
# Retrieval (Kendra)
# =======================
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

# =======================
# Geração (Bedrock Runtime)
# =======================
def generate_with_bedrock_runtime(question: str, snippets: list[str]) -> str:
    """
    Usa Bedrock Runtime diretamente (sem Knowledge Base) para gerar a resposta,
    condicionada estritamente ao contexto (snippets).
    """
    model_id = _resolve_model_id(MODEL_ARN or "anthropic.claude-v2:1")
    context = "\n\n---\n".join(snippets)
    if len(context) > 8000:
        context = context[:8000]

    # Suporte principal: Anthropic Claude v2.x (prompt estilo Human/Assistant)
    if model_id.startswith("anthropic.claude-2") or model_id.startswith("anthropic.claude-v2"):
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
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body)
        )
        payload = json.loads(resp["body"].read())
        return (payload.get("completion") or "").strip()

    # (Opcional) modelos novos podem usar outro schema; fallback simples:
    # Tente tratar como Claude 3 messages (se alguém trocar o MODEL_ARN mais tarde).
    body = {
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": f"{SYSTEM_INSTRUCTIONS}\n\nCONTEXTO:\n{context}\n\nPERGUNTA: {question}"}
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
    # Claude 3 costuma retornar em payload['output'][0]['content'][0]['text'] ou 'output_text'
    return (payload.get("output_text")
            or payload.get("generation")
            or "").strip()

# =======================
# Routes
# =======================
@app.get("/ping")
def ping():
    return jsonify({"ok": True})

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
