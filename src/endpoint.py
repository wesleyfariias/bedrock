# endpoint.py
import os
import re
import json
import logging
from typing import List, Tuple, Dict, Any

import boto3
from botocore.exceptions import ClientError
from flask import Flask, request, jsonify

# -----------------------------
# Config
# -----------------------------
REGION = os.getenv("BEDROCK_REGION", "us-east-1")
MODEL_ID = os.getenv("MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
# Separe múltiplos fallbacks por vírgula
MODEL_FALLBACKS: List[str] = [
    m.strip() for m in os.getenv("MODEL_FALLBACKS", "").split(",") if m.strip()
]

KENDRA_INDEX_ID = os.getenv("KENDRA_INDEX_ID", "")

# Afinando comportamento
TOP_K = int(os.getenv("KENDRA_TOP_K", "8"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "12000"))  # limita prompt
TEXT_MAX_TOKENS = int(os.getenv("TEXT_MAX_TOKENS", "1400"))
TEXT_TEMPERATURE = float(os.getenv("TEXT_TEMPERATURE", "0.5"))
JSON_MAX_TOKENS = int(os.getenv("JSON_MAX_TOKENS", "1400"))
JSON_TEMPERATURE = float(os.getenv("JSON_TEMPERATURE", "0.3"))

# Logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("hybrid-backend")

# AWS clients
brt = boto3.client("bedrock-runtime", region_name=REGION)
kendra = boto3.client("kendra", region_name=REGION)

app = Flask(__name__)
logger.info(f"[Boot] region={REGION} model_id={MODEL_ID} fallbacks={MODEL_FALLBACKS} kendra_index={KENDRA_INDEX_ID}")


# -----------------------------
# Kendra 查询
# -----------------------------
def kendra_search(query: str, top_k: int = TOP_K) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Busca no Kendra, retorna (trechos, fontes). Não levanta exceção."""
    if not KENDRA_INDEX_ID:
        return [], []
    try:
        res = kendra.query(indexId=KENDRA_INDEX_ID, queryText=query, pageSize=top_k)
    except Exception as e:
        logger.warning(f"[Kendra] query error: {e}")
        return [], []

    ctx_chunks: List[str] = []
    sources: List[Dict[str, Any]] = []
    for item in res.get("ResultItems", []):
        # Trecho
        excerpt = item.get("DocumentExcerpt", {})
        if "Text" in excerpt:
            ctx_chunks.append(excerpt["Text"])

        # Título
        title = (item.get("DocumentTitle") or {}).get("Text") or "Fonte sem título"

        # URL
        link = None
        for attr in item.get("DocumentAttributes", []):
            if attr.get("Key") == "DocumentURI":
                sv = attr.get("Value", {}).get("StringValue")
                if sv:
                    link = sv
                    break

        sources.append({"title": title, "url": link})

    # Dedup simples de fontes (title, url)
    seen = set()
    deduped = []
    for s in sources:
        key = (s.get("title"), s.get("url"))
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    return ctx_chunks, deduped


# -----------------------------
# Prompts
# -----------------------------
HYBRID_MARKDOWN_INSTRUCTION = """
Você é um assistente em PORTUGUÊS (estilo ChatGPT) com acesso opcional a uma BASE DE CONHECIMENTO (KB via Kendra).

POLÍTICA DE USO DA KB
- Use a KB para fatos específicos (regras, IDs, decisões, métricas). Não invente fatos/IDs que não estejam no contexto.
- Se algo não estiver na KB, complemente com conhecimento geral e boas práticas (sem criar números/IDs reais).
- Se usar a KB, inclua ao final uma seção **Fontes** com as URIs/links disponíveis. Se não usar a KB, escreva **Fontes: (sem fontes da KB)**.

FORMATO
- Responda em **Markdown**, claro e objetivo (listas, passos, trechos de código quando útil).
- NÃO use JSON a menos que o usuário peça explicitamente ou a tarefa exija saída estruturada.

OBJETIVO
- Responder perguntas gerais (explicar, resumir, escrever, criticar, planejar, dar passos, gerar código), combinando KB + conhecimento técnico.
"""

HYBRID_STRUCTURED_INSTRUCTION = """
Você é um assistente em PORTUGUÊS que deve produzir **SAÍDA ESTRUTURADA em JSON válido** quando a tarefa exigir artefatos de QA (ex.: casos de teste, critérios, checklist) ou quando o usuário pedir explicitamente JSON.

REGRAS
- Use a KB para fatos (IDs, regras, decisões). Não invente fatos da KB; se faltar, proponha como [AI] sem IDs falsos.
- Retorne APENAS um **JSON** que siga o schema abaixo (sem texto fora do JSON):

{
  "summary": "string",
  "artifacts": {
    "test_cases": [
      {
        "id": "TC-001",
        "title": "string",
        "type": "functional|negative|nonfunctional",
        "steps": ["..."],
        "expected_result": "string",
        "tags": ["UI","API","Regression"],
        "traceability": ["US-1234","AC-1"]
      }
    ],
    "acceptance_criteria": ["AC-1: ...", "AC-2: ..."],
    "validation_checklist": ["..."],
    "risks": ["..."],
    "open_questions": ["..."]
  },
  "sources": [{"title":"string","url":"string|null"}]
}

- Gere 5–12 casos se o pedido for sobre casos de teste. Se não for, ajuste os campos de 'artifacts' conforme a tarefa.
- Se a KB não retornar nada, produza conteúdo coerente e use "sources": [].
"""


def build_markdown_prompt(user_msg: str, context: str) -> str:
    return f"""{HYBRID_MARKDOWN_INSTRUCTION}

[PERGUNTA DO USUÁRIO]
{user_msg}

[CONTEXTO DA KB]
{context if context.strip() else "(sem resultados)"}"""

def build_structured_prompt(user_msg: str, context: str) -> str:
    return f"""{HYBRID_STRUCTURED_INSTRUCTION}

[PERGUNTA DO USUÁRIO]
{user_msg}

[CONTEXTO DA KB]
{context if context.strip() else "(sem resultados)"}"""


# -----------------------------
# Heurística: quando queremos JSON?
# -----------------------------
STRUCTURED_PATTERNS = [
    r"\bcasos?\s+de\s+teste\b",
    r"\btest\s*cases?\b",
    r"\bcrit[ée]rios?\s+de\s+aceita[cç][aã]o\b",
    r"\bchecklist\b",
    r"\bplano\s+de\s+teste\b",
    r"\bmatriz\s+de\s+teste\b",
    r"\bretorne?\s+json\b",
    r"\bformato\s+json\b",
]
STRUCTURED_REGEXES = [re.compile(p, re.IGNORECASE) for p in STRUCTURED_PATTERNS]

def wants_structured(user_msg: str) -> bool:
    um = (user_msg or "")
    return any(rx.search(um) for rx in STRUCTURED_REGEXES)


# -----------------------------
# Bedrock helpers (converse + fallback)
# -----------------------------
def try_converse_any(prompt: str, max_tokens: int, temperature: float) -> Tuple[str, str]:
    """
    Tenta chamar bedrock.converse com o MODEL_ID principal e fallbacks.
    Retorna (texto, model_usado). Lança exceção se todos falharem.
    """
    candidates = [MODEL_ID] + MODEL_FALLBACKS
    last_err = None
    for mid in candidates:
        try:
            resp = brt.converse(
                modelId=mid,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )
            parts = resp["output"]["message"]["content"]
            txt = "".join(p.get("text", "") for p in parts)
            logger.info(f"[Bedrock] used model: {mid}")
            return txt, mid
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            msg = e.response.get("Error", {}).get("Message")
            logger.warning(f"[Bedrock] {mid} failed: {code} - {msg}")
            last_err = e
            # AccessDenied / ModelNotReady / Throttling → tenta próximo
            if code in ("AccessDeniedException", "ModelNotReadyException", "ThrottlingException"):
                continue
            # Outros erros: não adianta tentar o próximo (provavelmente de sintaxe)
            raise
    if last_err:
        raise last_err
    raise RuntimeError("Nenhum modelo disponível para converse")


def converse_text(prompt: str, max_tokens: int = TEXT_MAX_TOKENS, temperature: float = TEXT_TEMPERATURE) -> str:
    raw, used_model = try_converse_any(prompt, max_tokens, temperature)
    return raw

def converse_json(prompt: str, max_tokens: int = JSON_MAX_TOKENS, temperature: float = JSON_TEMPERATURE) -> Dict[str, Any]:
    raw, used_model = try_converse_any(prompt, max_tokens, temperature)
    # Tenta parsear direto
    try:
        return json.loads(raw)
    except Exception:
        # Heurística: extrai o maior bloco { ... }
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(raw[s:e+1])
            except Exception:
                pass
        # Fallback mínimo
        return {"summary": raw[:800], "artifacts": {}, "sources": []}


# -----------------------------
# Utilidades
# -----------------------------
def build_context_text(chunks: List[str]) -> str:
    if not chunks:
        return ""
    text = "\n\n---\n\n".join(chunks[:TOP_K])
    # Limita tamanho
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n\n---(truncado)---"
    return text

def append_sources_if_missing(answer_md: str, sources: List[Dict[str, Any]]) -> str:
    """Se o modelo não colocou 'Fontes' e temos KB, anexa no final."""
    if not sources:
        # Se não há fontes e não há menção, adicione linha neutra para padronizar
        if "Fontes" not in answer_md:
            return answer_md.rstrip() + "\n\n**Fontes**\n(sem fontes da KB)\n"
        return answer_md
    # Já incluiu 'Fontes'? mantenha
    if "Fontes" in answer_md:
        return answer_md
    lines = []
    for s in sources:
        if s.get("url"):
            lines.append(f"- {s['title']} — {s['url']}")
        else:
            lines.append(f"- {s['title']}")
    return answer_md.rstrip() + "\n\n**Fontes**\n" + "\n".join(lines) + "\n"

def merge_sources(primary: List[Dict[str, Any]], extra: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {(s.get("title"), s.get("url")) for s in primary}
    merged = primary[:]
    for s in extra:
        key = (s.get("title"), s.get("url"))
        if key not in seen:
            seen.add(key)
            merged.append(s)
    return merged


# -----------------------------
# HTTP endpoints
# -----------------------------
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

@app.get("/_debug/config")
def debug_config():
    return jsonify({
        "region": REGION,
        "model_id": MODEL_ID,
        "fallbacks": MODEL_FALLBACKS,
        "kendra_index_id": KENDRA_INDEX_ID,
        "top_k": TOP_K,
        "max_context_chars": MAX_CONTEXT_CHARS
    })


@app.post("/chat")
def chat():
    data = request.get_json(force=True) or {}
    user_msg = (data.get("message") or "").strip()

    if not user_msg:
        return jsonify({"text": "Mensagem vazia."}), 200

    # 1) Busca KB
    ctx_chunks, kb_sources = kendra_search(user_msg)
    context = build_context_text(ctx_chunks)

    # 2) Decide formato da resposta
    structured = wants_structured(user_msg)

    try:
        if structured:
            # JSON estruturado (só quando a tarefa pede)
            prompt = build_structured_prompt(user_msg, context)
            result = converse_json(prompt, max_tokens=JSON_MAX_TOKENS, temperature=JSON_TEMPERATURE)
            # fontes: mescla as do modelo com as da KB (sem duplicar)
            result_sources = result.get("sources") or []
            result["sources"] = merge_sources(result_sources, kb_sources)
            return jsonify(result), 200

        # Markdown genérico (estilo ChatGPT com KB)
        prompt = build_markdown_prompt(user_msg, context)
        answer = converse_text(prompt, max_tokens=TEXT_MAX_TOKENS, temperature=TEXT_TEMPERATURE)
        answer = append_sources_if_missing(answer, kb_sources)
        return jsonify({"text": answer, "sources": kb_sources}), 200

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        msg = e.response.get("Error", {}).get("Message")
        logger.error(f"[BedrockError] {code}: {msg}")
        # Devolve erro amigável (front mostra na bolha)
        return jsonify({
            "text": (
                "Falha ao invocar o modelo do Bedrock.\n\n"
                f"**Erro**: {code}\n"
                f"**Detalhe**: {msg}\n\n"
                "Verifique se o MODEL_ID está habilitado nesta região ou defina MODEL_FALLBACKS."
            ),
            "sources": []
        }), 200
    except Exception as ex:
        logger.exception("[ServerError] unhandled")
        return jsonify({"text": f"Erro inesperado no servidor: {ex}", "sources": []}), 200


# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    # Dica: use host=0.0.0.0 para permitir acesso externo (Next com rewrite)
    app.run(host="0.0.0.0", port=8081, debug=True)
