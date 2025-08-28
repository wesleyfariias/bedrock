from flask import Flask, request, jsonify
import os, re, json, traceback, boto3
from botocore.exceptions import BotoCoreError, ClientError

# ====================== ENV / REGIÕES ======================
def _env(name, default=""):
    return (os.getenv(name, default) or "").strip()

BEDROCK_REGION   = _env("BEDROCK_REGION", "us-east-1")
KENDRA_REGION    = _env("KENDRA_REGION", _env("AWS_REGION", "us-east-1"))

KENDRA_INDEX_ID  = _env("KENDRA_INDEX_ID")            # obrigatório p/ retrieve
KENDRA_LANGUAGE  = _env("KENDRA_LANGUAGE")            # opcional (ex.: "pt")
ALLOW_CREATIVE   = _env("ALLOW_CREATIVE", "true").lower() == "true"

MODEL_ID         = _env("MODEL_ID")                   # opcional (on-demand)
MODEL_ARN        = _env("MODEL_ARN")                  # opcional (provisioned/profile)
DEFAULT_ON_DEMAND = "anthropic.claude-v2:1"

def _resolve_model_identifier() -> str:
    arn = MODEL_ARN
    if arn.startswith("arn:aws:bedrock:"):
        if any(p in arn for p in (":provisioned-model/", ":application-inference-profile/", ":system-inference-profile/")):
            return arn  # pode usar ARN direto
        if "/foundation-model/" in arn:
            return arn.split("/foundation-model/")[-1]
    return MODEL_ID or DEFAULT_ON_DEMAND

MODEL_IDENTIFIER = _resolve_model_identifier()

# ====================== CLIENTES ======================
bedrock_rt = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
def kendra_client():
    return boto3.client("kendra", region_name=KENDRA_REGION)

# ====================== INSTRUÇÕES ======================
SYSTEM_INSTRUCTIONS_STRICT = (
    "Você é uma IA que responde em português, de forma objetiva, "
    "USANDO EXCLUSIVAMENTE o CONTEXTO fornecido. "
    "Se o contexto não trouxer evidências suficientes, responda exatamente: "
    "\"Não encontrei informações sobre isso na base de conhecimento.\" "
    "Ao final, inclua uma seção 'Fontes' listando as URIs se houver."
)

SYSTEM_INSTRUCTIONS_CREATIVE = (
    "Você é uma IA que responde em português, de forma objetiva. "
    "Use o CONTEXTO fornecido quando existir. "
    "Se o pedido envolver criar/propor/exemplificar, você PODE extrapolar e gerar conteúdo novo "
    "com base em boas práticas. Deixe claro o que veio do CONTEXTO e o que é Proposta. "
    "Se não houver contexto, ainda assim produza uma Proposta. "
    "Ao final, inclua 'Fontes' com URIs quando usar o CONTEXTO; caso contrário, escreva 'Fontes: (proposta, sem fontes)'."
)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ====================== HELPERS ======================
def missing_env():
    miss = []
    if not KENDRA_INDEX_ID:  miss.append("KENDRA_INDEX_ID")
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
    m = re.search(r'\b([A-Za-z]{1,8})[-\s]?(\d{2,10})\b', q)
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

# ====================== RETRIEVAL (KENDRA) ======================
def _extract_excerpt(item: dict) -> str:
    """Tenta extrair um trecho de texto independente do tipo do item."""
    # 1) DocumentExcerpt.Text
    excerpt = item.get("DocumentExcerpt", {}).get("Text") or ""
    if excerpt:
        return excerpt

    # 2) AdditionalAttributes: AnswerText / PassageText
    for aa in item.get("AdditionalAttributes", []):
        if aa.get("ValueType") == "TEXT_WITH_HIGHLIGHTS_VALUE":
            tw = aa.get("Value", {}).get("TextWithHighlightsValue", {})
            if tw.get("Text"):
                return tw["Text"]

        # alguns SDKs usam Key/Value sem ValueType
        if aa.get("Key") in ("AnswerText", "PassageText"):
            tw = aa.get("Value", {}).get("TextWithHighlightsValue", {})
            if tw.get("Text"):
                return tw["Text"]

    return ""

def retrieve_kendra(query: str, top_k: int = 12):
    cli = kendra_client()
    items = []
    for qv in make_query_variants(query):
        resp = None
        last_err = None

        # tenta com e sem LanguageCode (se KENDRA_LANGUAGE vazio, só sem)
        attempts = [False] if not KENDRA_LANGUAGE else [True, False]
        for use_lang in attempts:
            try:
                params = {
                    "IndexId": KENDRA_INDEX_ID,
                    "QueryText": qv,
                    "PageSize": min(top_k, 50),
                }
                if use_lang:
                    params["LanguageCode"] = KENDRA_LANGUAGE
                resp = cli.query(**params)
                break
            except Exception as e:
                last_err = e

        if resp is None:
            print(f"[KENDRA] query FAILED for '{qv}' (lang={'on' if KENDRA_LANGUAGE else 'off'}): {last_err}")
            continue

        for it in resp.get("ResultItems", []):
            if it.get("Type") not in ("DOCUMENT", "ANSWER", "QUESTION_ANSWER"):
                continue
            uri = it.get("DocumentURI") or ""
            excerpt = _extract_excerpt(it).strip()
            if not excerpt:
                continue
            items.append({"uri": uri or "(sem URI)", "excerpt": excerpt, "score": None})

        if len(items) >= top_k:
            break

    # dedup por (uri, início do texto)
    uniq, seen = [], set()
    for x in items:
        key = (x["uri"], x["excerpt"][:120])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(x)
    print(f"[KENDRA] retrieved {len(uniq[:top_k])} items for '{query}'")
    return uniq[:top_k]

# ====================== GERAÇÃO (BEDROCK RUNTIME) ======================
def generate_with_bedrock_runtime(question: str, snippets: list[str], creative: bool) -> str:
    """Gera resposta condicionada ao contexto (snippets). Suporta Claude 2 (prompt) e 3 (messages)."""
    model_id = MODEL_IDENTIFIER
    instructions = SYSTEM_INSTRUCTIONS_CREATIVE if creative else SYSTEM_INSTRUCTIONS_STRICT

    context = "\n\n---\n".join(snippets)
    # limitar contexto para evitar payload gigante
    if len(context) > 12000:
        context = context[:12000]

    # Modelos Claude 3 (messages)
    if model_id.startswith("arn:aws:bedrock:") or model_id.startswith("anthropic.claude-3"):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": (
                        f"{instructions}\n\n"
                        f"CONTEXTO:\n{context}\n\n"
                        f"PERGUNTA: {question}\n"
                    )}
                ]}
            ],
            "max_tokens": 900,
            "temperature": 0.2
        }
        resp = bedrock_rt.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body)
        )
        payload = json.loads(resp["body"].read())
        # tentativas de extração
        return (payload.get("output_text")
                or payload.get("generation")
                or (payload.get("content") or [{}])[0].get("text", "")
                or "").strip()

    # Claude 2.x (prompt)
    prompt = (
        f"\n\nHuman: {instructions}\n\n"
        f"CONTEXTO:\n{context}\n\n"
        f"PERGUNTA: {question}\n\n"
        "Responda de forma objetiva.\n"
        "Ao final escreva uma seção 'Fontes' listando as URIs se houver.\n"
        "\n\nAssistant:"
    )
    body = {
        "prompt": prompt,
        "max_tokens_to_sample": 900,
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

# ====================== ROTAS ======================
@app.get("/ping")
def ping():
    return jsonify({"ok": True})

@app.get("/env")
def env_view():
    return jsonify({
        "region_bedrock": BEDROCK_REGION,
        "region_kendra": KENDRA_REGION,
        "kendra_index_id": KENDRA_INDEX_ID,
        "kendra_language": KENDRA_LANGUAGE or "(none)",
        "model": MODEL_IDENTIFIER,
        "allow_creative": ALLOW_CREATIVE,
    })

@app.get("/kendra-stats")
def kendra_stats():
    try:
        cli = kendra_client()
        r = cli.describe_index(IndexId=KENDRA_INDEX_ID)
        stats = r.get("IndexStatistics", {}).get("TextDocumentStatistics", {})
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/kendra-debug")
def kendra_debug():
    q = (request.args.get("q") or "").strip()
    hits = retrieve_kendra(q, 12) if q else []
    return jsonify({"query": q, "hits": hits})

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
        mode = (data.get("mode") or "strict").lower()
        k = int(data.get("k") or 12)

        if not user_msg:
            return jsonify({"error": "Body precisa conter JSON com o campo 'message'."}), 400

        # força strict se ALLOW_CREATIVE = false
        if mode == "creative" and not ALLOW_CREATIVE:
            mode = "strict"

        # 1) Retrieve no Kendra
        k_hits = retrieve_kendra(user_msg, k)
        citations = [{"uri": h["uri"], "score": h["score"]} for h in k_hits]
        snippets  = [h["excerpt"] for h in k_hits]

        # 2) Lógica de resposta
        if mode == "strict":
            if not snippets:
                return jsonify({
                    "answer": "Não encontrei informações sobre isso na base de conhecimento.",
                    "citations": []
                }), 200
            answer = generate_with_bedrock_runtime(user_msg, snippets, creative=False)
            if not answer.strip():
                answer = "Não encontrei informações sobre isso na base de conhecimento."
            return jsonify({"answer": answer, "citations": citations, "debug": {"mode": mode, "snippets": len(snippets)}}), 200

        # CREATIVE
        if snippets:
            # Criativo COM contexto -> gera respeitando CONTEXTO e pode propor extras
            answer = generate_with_bedrock_runtime(user_msg, snippets, creative=True)
            return jsonify({"answer": answer, "citations": citations, "debug": {"mode": mode, "snippets": len(snippets)}}), 200
        else:
            # Criativo SEM contexto -> Proposta sem fontes
            return jsonify({
                "answer": "Proposta:\n\n" + generate_with_bedrock_runtime(user_msg, [], creative=True),
                "citations": [],
                "debug": {"mode": mode, "snippets": 0}
            }), 200

    except (BotoCoreError, ClientError) as e:
        return jsonify({"error": "AWS error", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)
