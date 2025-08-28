from flask import Flask, request, jsonify
import os, re, json, traceback, boto3
from botocore.exceptions import BotoCoreError, ClientError

# ========= ENV & REGIÕES =========
def _env(name, default=""):
    return (os.getenv(name, default) or "").strip()

BEDROCK_REGION   = _env("BEDROCK_REGION", "us-east-1")                 # região do Bedrock
KENDRA_REGION    = _env("KENDRA_REGION", _env("AWS_REGION", "us-east-1"))  # região do índice Kendra
KENDRA_LANGUAGE  = _env("KENDRA_LANGUAGE", "pt")                       # idioma de consulta do Kendra (pt/en/auto)
KENDRA_INDEX_ID  = _env("KENDRA_INDEX_ID")                             # OBRIGATÓRIO p/ retrieve
MODEL_ID         = _env("MODEL_ID")                                    # opcional (on-demand)
MODEL_ARN        = _env("MODEL_ARN")                                   # opcional (provisioned/profile)

# Fallback on-demand atual (ajuste se quiser outro)
DEFAULT_ON_DEMAND = "anthropic.claude-v2:1"

def _resolve_model_identifier() -> str:
    """
    Retorna um identificador aceito pelo Bedrock:
    - Se for ARN de provisioned/profile => usa o ARN inteiro
    - Se for ARN de foundation-model => extrai o modelId (…/foundation-model/<modelId>)
    - Senão usa MODEL_ID; se vazio, usa fallback on-demand
    """
    arn = MODEL_ARN
    if arn.startswith("arn:aws:bedrock:"):
        if any(p in arn for p in (":provisioned-model/", ":application-inference-profile/", ":system-inference-profile/")):
            return arn
        if "/foundation-model/" in arn:
            return arn.split("/foundation-model/")[-1]
    return MODEL_ID or DEFAULT_ON_DEMAND

MODEL_IDENTIFIER = _resolve_model_identifier()

# ========= CLIENTES AWS =========
bedrock_rt = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
def kendra_client():
    return boto3.client("kendra", region_name=KENDRA_REGION)

# ========= PROMPTS =========
SYSTEM_INSTRUCTIONS_STRICT = (
    "Você é uma IA que responde em português, de forma objetiva, usando EXCLUSIVAMENTE o CONTEXTO.\n"
    "Se o contexto não trouxer evidências suficientes, responda exatamente: "
    "\"Não encontrei informações sobre isso na base de conhecimento.\" "
    "Inclua ao final uma seção 'Fontes' quando houver URIs."
)

SYSTEM_INSTRUCTIONS_CREATIVE = (
    "Você é uma IA que responde em português, de forma objetiva. "
    "Use o CONTEXTO quando existir; porém, quando a solicitação envolver criar/propor/exemplificar "
    "(ex.: 'crie', 'proponha', 'exemplo', 'cenário'), você PODE EXTRAPOLAR e gerar conteúdo novo com boas práticas. "
    "Deixe claro o que veio do CONTEXTO e o que é Proposta. "
    "Se não houver CONTEXTO relevante, ainda assim produza uma Proposta. "
    "Quando usar trechos do CONTEXTO, inclua uma seção 'Fontes' com as URIs; "
    "se não houver, escreva 'Fontes: (proposta, sem fontes)'."
)

# ========= FLASK APP =========
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ========= HELPERS =========
def missing_env():
    miss = []
    if not KENDRA_INDEX_ID:  miss.append("KENDRA_INDEX_ID")
    return miss

@app.after_request
def add_id_header(resp):
    resp.headers["X-Service"]        = "flask-kendra"
    resp.headers["X-Bedrock-Region"] = BEDROCK_REGION
    resp.headers["X-Kendra-Region"]  = KENDRA_REGION
    resp.headers["X-Model"]          = MODEL_IDENTIFIER
    return resp

def make_query_variants(q: str):
    """Variações para IDs do tipo US-5270, US 5270, US5270, 5270 etc."""
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

# ========= RETRIEVAL: Kendra =========
def retrieve_kendra(query: str, top_k: int = 12):
    cli = kendra_client()
    items = []
    for qv in make_query_variants(query):
        try:
            resp = cli.query(
                IndexId=KENDRA_INDEX_ID,
                QueryText=qv,
                PageSize=min(top_k, 50),
                LanguageCode=KENDRA_LANGUAGE or "pt"
            )
        except (BotoCoreError, ClientError) as e:
            # falha na query -> pula variação
            continue

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
                            excerpt = tw["Text"]; break
            if not excerpt:
                continue
            items.append({
                "uri": uri or "(sem URI)",
                "excerpt": excerpt.strip(),
                "score": None
            })
        if len(items) >= top_k:
            break

    # dedup por (uri, início do texto)
    uniq, seen = [], set()
    for x in items:
        key = (x["uri"], x["excerpt"][:96])
        if key in seen: 
            continue
        seen.add(key)
        uniq.append(x)
    return uniq[:top_k]

# ========= GERAÇÃO: Bedrock Runtime (Claude v2 por padrão) =========
def _is_claude3(model_id: str) -> bool:
    """Detecta se é um modelo Claude 3 (por id/arn contendo 'claude-3')."""
    return "claude-3" in model_id

def generate_with_bedrock_runtime(question: str, snippets: list[str], mode: str = "strict") -> str:
    """
    Gera resposta com Bedrock Runtime (Anthropic).
    - mode = 'strict' => só contexto
    - mode = 'creative' => pode propor conteúdo (Proposta)
    """
    model_id = MODEL_IDENTIFIER
    context = "\n\n---\n".join(snippets) if snippets else ""
    if len(context) > 8000:
        context = context[:8000]

    sys = SYSTEM_INSTRUCTIONS_CREATIVE if mode == "creative" else SYSTEM_INSTRUCTIONS_STRICT

    # Para simplificar e manter compatibilidade com claude-v2:1, usamos o formato "prompt"
    prompt = (
        f"\n\nHuman: {sys}\n\n"
        f"CONTEXTO:\n{context or '(vazio)'}\n\n"
        f"PERGUNTA: {question}\n\n"
        "Responda de forma concisa. Quando extrapolar, rotule a(s) seção(ões) como 'Proposta'. "
        "Inclua ao final uma seção 'Fontes'.\n"
        "\n\nAssistant:"
    )

    body = {
        "prompt": prompt,
        "max_tokens_to_sample": 900,
        "temperature": 0.3 if mode == "creative" else 0.2,
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

# ========= ROTAS =========
@app.get("/ping")
def ping():
    return jsonify({
        "ok": True,
        "bedrock_region": BEDROCK_REGION,
        "kendra_region": KENDRA_REGION,
        "kendra_language": KENDRA_LANGUAGE,
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
        mode = (data.get("mode") or "strict").lower()  # "strict" | "creative"

        if not user_msg:
            return jsonify({"error": "Body precisa conter JSON com o campo 'message'."}), 400

        # 1) Retrieve no Kendra
        k_hits = retrieve_kendra(user_msg, k)
        citations = [{"uri": h["uri"], "score": h["score"]} for h in k_hits]
        snippets  = [h["excerpt"] for h in k_hits]

        # 2) Sem snippets:
        if not snippets:
            if mode == "creative":
                # gera mesmo sem contexto (Proposta)
                answer = generate_with_bedrock_runtime(user_msg, [], mode=mode) or "Proposta: sem conteúdo gerado."
                return jsonify({"answer": answer, "citations": []}), 200
            else:
                return jsonify({
                    "answer": "Não encontrei informações sobre isso na base de conhecimento.",
                    "citations": []
                }), 200

        # 3) Geração usando (ou não) extrapolação
        answer = generate_with_bedrock_runtime(user_msg, snippets, mode=mode) or (
            "Não encontrei informações sobre isso na base de conhecimento."
            if mode == "strict" else "Proposta: sem conteúdo gerado."
        )
        return jsonify({"answer": answer, "citations": citations}), 200

    except (BotoCoreError, ClientError) as e:
        return jsonify({"error": "AWS error", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

# ========= ENDPOINTS DE DIAGNÓSTICO (úteis) =========
@app.get("/env")
def env_info():
    return jsonify({
        "region_bedrock": BEDROCK_REGION,
        "region_kendra": KENDRA_REGION,
        "kendra_language": KENDRA_LANGUAGE,
        "kendra_index_id": KENDRA_INDEX_ID,
        "model": MODEL_IDENTIFIER
    })

@app.get("/kendra-stats")
def kendra_stats():
    try:
        cli = kendra_client()
        idx = cli.describe_index(Id=KENDRA_INDEX_ID)
        stats = idx.get("IndexStatistics", {}).get("TextDocumentStatistics", {})
        return jsonify(stats), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/kendra-debug")
def kendra_debug():
    try:
        q = request.args.get("q") or "SOS Mulher"
        hits = retrieve_kendra(q, 5)
        return jsonify({"query": q, "hits": [{"uri": h["uri"], "excerpt": h["excerpt"][:180]} for h in hits]}), 200
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

# ========= MAIN =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
