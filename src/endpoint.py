import os, json, boto3
from flask import Flask, request, jsonify

REGION = os.getenv("BEDROCK_REGION", "us-east-1")
MODEL_ID = os.getenv("MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
KENDRA_INDEX_ID = os.getenv("KENDRA_INDEX_ID")

brt = boto3.client("bedrock-runtime", region_name=REGION)
kendra = boto3.client("kendra", region_name=REGION)

app = Flask(__name__)

def kendra_search(query: str, top_k: int = 8):
    if not KENDRA_INDEX_ID:
        return [], []
    try:
        res = kendra.query(indexId=KENDRA_INDEX_ID, queryText=query, pageSize=top_k)
    except Exception:
        return [], []
    ctx_chunks, sources = [], []
    for item in res.get("ResultItems", []):
        if "DocumentExcerpt" in item and "Text" in item["DocumentExcerpt"]:
            ctx_chunks.append(item["DocumentExcerpt"]["Text"])
        title = (item.get("DocumentTitle") or {}).get("Text") or "Fonte sem título"
        link = None
        for attr in item.get("DocumentAttributes", []):
            if attr.get("Key") == "DocumentURI":
                sv = attr.get("Value", {}).get("StringValue")
                if sv:
                    link = sv
                    break
        sources.append({"title": title, "url": link})
    return ctx_chunks, sources

HYBRID_JSON_INSTRUCTION = """
Você é um assistente técnico em PORTUGUÊS que combina BASE DE CONHECIMENTO (KB via Kendra) e conhecimento técnico geral.

OBJETIVO
- Responder ao usuário gerando artefatos úteis (ex.: casos de teste, critérios, checklist), usando fatos da KB e completando criativamente o que faltar.

POLÍTICA DE EVIDÊNCIAS
- PRIORIZE a KB para fatos (regras existentes, IDs, trechos literais, decisões já tomadas).
- NUNCA invente fatos da KB (IDs, métricas, decisões). Se faltar, crie como proposta técnica [AI].
- Rotule cada item com sua origem explícita: use “[KB]” quando vier da base, “[AI]” quando for criação/boa prática.

FORMATO DE SAÍDA (JSON VÁLIDO)
- Responda APENAS com um JSON que siga o schema abaixo (sem texto fora do JSON):

{
  "summary": "string (resumo dos pontos-chave; rotule trechos com [KB] ou [AI])",
  "artifacts": {
    "test_cases": [
      {
        "id": "TC-001",
        "title": "string [AI]",
        "type": "functional|negative|nonfunctional",
        "steps": ["passo 1", "passo 2", "..."],
        "expected_result": "string",
        "tags": ["UI","API","Regression"],
        "traceability": ["US-1234","AC-1"]  // referencie IDs reais da KB quando existirem; não invente
      }
    ],
    "acceptance_criteria": ["AC-1: ... [KB|AI]", "AC-2: ... [KB|AI]"],
    "validation_checklist": ["item ... [AI]", "..."],
    "risks": ["risco ... [AI]"],
    "open_questions": ["pergunta ... [AI]"]
  },
  "sources": [
    {"title":"string","url":"string|null"}
  ]
}

REGRAS DE CONTEÚDO
- Gere 5–12 casos de teste variados (funcionais, negativos e não-funcionais) quando solicitado.
- Traga exemplos práticos e critérios mensuráveis (ex.: tempos, formatos, máscaras) somente se estiverem na KB [KB] ou como proposta [AI].
- Em conflitos entre KB e suposições, prevalece a KB (explique no resumo).
- Se a KB não retornar nada, produza tudo como [AI] e use "sources": [].

ESTILO
- Objetivo, técnico e direto.
- Português do Brasil.
- Sem rodeios; nada fora do JSON.

ENTRADA DO USUÁRIO:
{user_msg}

CONTEXTO DA KB (texto bruto; pode estar vazio):
{context}
"""


def build_hybrid_prompt(user_msg: str, context: str) -> str:
    return f"""{HYBRID_JSON_INSTRUCTION}

[ENTRADA DO USUÁRIO]
{user_msg}

[CONTEXTO DA KB]
{context if context.strip() else "(sem resultados)"}"""

def converse_json(prompt: str, max_tokens: int = 1400, temperature: float = 0.3):
    # Pede JSON e tenta fazer "auto-repair" se vier com texto extra
    resp = brt.converse(
        modelId=MODEL_ID,
        messages=[{"role":"user","content":[{"text":prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    raw = "".join(p.get("text","") for p in resp["output"]["message"]["content"])
    # Tentativa direta
    try:
        return json.loads(raw)
    except Exception:
        # heurística simples: procurar primeiro { ... } maior
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = raw[start:end+1]
            try:
                return json.loads(snippet)
            except Exception:
                pass
        # fallback mínimo
        return {"summary": raw[:400], "artifacts": {}, "sources": []}

@app.post("/chat")
def chat():
    data = request.get_json(force=True)
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "Mensagem vazia."}), 400

    # 1) Busca KB
    ctx_chunks, sources = kendra_search(user_msg)
    context = "\n\n---\n\n".join(ctx_chunks)

    # 2) Prompt híbrido único
    prompt = build_hybrid_prompt(user_msg, context)

    # 3) Gera JSON estruturado
    result = converse_json(prompt)

    # 4) Garante que as fontes retornem (mantemos as da KB)
    if "sources" not in result or not isinstance(result["sources"], list) or len(result["sources"]) == 0:
        result["sources"] = sources
    else:
        # mescla sem duplicar
        seen = {(s.get("title"), s.get("url")) for s in result["sources"]}
        for s in sources:
            key = (s.get("title"), s.get("url"))
            if key not in seen:
                result["sources"].append(s)

    return jsonify(result), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=True)
