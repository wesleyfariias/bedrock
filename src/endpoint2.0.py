# app.py
import os
import json
import re
import datetime as dt
from typing import Optional, List, Dict

import boto3
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# --- Config ---
REGION = os.getenv("AWS_REGION", "us-east-1")
S3_KNOW = os.getenv("S3_KNOW", "pmesp-ai-knowledge")
S3_OUT = os.getenv("S3_OUT", "pmesp-ai-outputs")
MODEL_ID = os.getenv("MODEL_ID", "anthropic.claude-v2:1")  # Claude 2.1
KENDRA_INDEX_ID = os.getenv("KENDRA_INDEX_ID")  # pode estar vazio

# --- AWS clients ---
s3 = boto3.client("s3", region_name=REGION)
kendra = boto3.client("kendra", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# --- FastAPI app ---
app = FastAPI(title="PMESP AI Orchestrator (S3/Kendra + Claude 2.1)")

# --- Util: busca no Kendra ---
def kendra_search(query: str, top_k: int = 8) -> List[Dict[str, str]]:
    if not KENDRA_INDEX_ID:
        # Sem índice configurado: retorna contexto vazio
        return []
    r = kendra.query(
        IndexId=KENDRA_INDEX_ID,
        QueryText=query,
        PageSize=top_k
    )
    chunks = []
    for item in r.get("ResultItems", []):
        if item.get("Type") in ("ANSWER", "DOCUMENT"):
            title = item.get("DocumentTitle", {}).get("Text", "")
            text = item.get("DocumentExcerpt", {}).get("Text", "")
            src = item.get("DocumentId", "")
            chunks.append({"title": title, "text": text, "source": src})
    return chunks

# --- Util: chamada ao Claude 2.1 (prompt/completion) ---
def bedrock_claude21(system: str, user_prompt: str, max_tokens: int = 2000, temperature: float = 0.2) -> str:
    """
    Claude 2.1 via Bedrock usa 'prompt' e retorna 'completion'.
    """
    prompt = f"\n\nHuman: {system}\n\n{user_prompt}\n\nAssistant:"
    body = {
        "prompt": prompt,
        "max_tokens_to_sample": max_tokens,
        "temperature": temperature,
        "stop_sequences": ["\n\nHuman:"]
    }
    try:
        resp = bedrock.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body)
        )
        out = json.loads(resp["body"].read())
        return out.get("completion", "").strip()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bedrock invoke error: {e}")

# --- Util: salvar no S3 (+ meta opcional) ---
def save_s3(text: str, key: str, meta: Optional[dict] = None) -> None:
    s3.put_object(Bucket=S3_OUT, Key=key, Body=text.encode("utf-8"))
    if meta:
        meta_key = key.rsplit(".", 1)[0] + ".meta.json"
        s3.put_object(
            Bucket=S3_OUT,
            Key=meta_key,
            Body=json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
        )

# --- System prompt canônico ---
SYSTEM = (
    "Você é o Assistente de Engenharia da PMESP.\n"
    "- Siga estritamente os templates oficiais e processos do contexto.\n"
    "- Se faltar dado, faça perguntas objetivas.\n"
    "- Ao final, acrescente seção 'Fontes' com os paths do contexto (quando houver).\n"
    "- Responda em português claro e direto, sem jargões desnecessários."
)

# --- Models de entrada ---
class GenStoryIn(BaseModel):
    objetivo: str
    contexto: Optional[str] = None
    approve: bool = False

# --- Endpoint: gerar User Story ---
@app.post("/gen/user-story")
def gen_user_story(inp: GenStoryIn):
    # 1) Recuperar contexto (Kendra)
    k_query = f"template user story padrão PMESP; {inp.objetivo} {inp.contexto or ''}"
    context = kendra_search(k_query, top_k=8)
    if context:
        ctx_text = "\n\n".join(
            f"# {c['title']}\n{c['text']}\n(Fonte:{c['source']})" for c in context
        )
        fontes_texto = "\n".join(f"- {c['source']}" for c in context)
    else:
        ctx_text = "(Sem contexto do Kendra disponível)"
        fontes_texto = "(Nenhuma fonte Kendra encontrada)"

    # 2) Montar prompt do usuário
    user_prompt = (
        "Contexto do Kendra (use como referência e cite as fontes ao final):\n"
        f"{ctx_text}\n\n"
        "Pedido: Gere uma USER STORY no padrão oficial (template), contendo:\n"
        "- Título\n"
        "- Descrição (HTML) autoexplicativa\n"
        "- Critérios de aceite em BDD (Given/When/Then)\n"
        "- Dependências e Riscos\n"
        "- Definição de Pronto (DoD)\n"
        "Se faltar algum dado, inclua uma seção 'Perguntas' com dúvidas objetivas.\n"
        "Inclua ao final uma seção 'Fontes' com os paths do contexto.\n"
    )

    # 3) Chamar Claude 2.1
    draft = bedrock_claude21(system=SYSTEM, user_prompt=user_prompt, max_tokens=2000, temperature=0.2)

    # Se não for para salvar, retornar prévia
    if not inp.approve:
        return {
            "preview": draft,
            "kendra_sources": [c["source"] for c in context],
            "notice": "Defina approve=true para gravar no S3."
        }

    # 4) Gravar no S3 com meta
    today = dt.datetime.now().strftime("%Y/%m/%d")
    # slug seguro do objetivo
    slug = re.sub(r"[^a-z0-9\-]+", "-", inp.objetivo.lower().strip().replace(" ", "-"))
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:64] or "story"
    key = f"{today}/user_story_{slug}.md"

    meta = {
        "tipo": "user_story",
        "objetivo": inp.objetivo,
        "contexto": inp.contexto,
        "kendra_hits": [c["source"] for c in context],
        "s3_knowledge_bucket": S3_KNOW,
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "model_id": MODEL_ID
    }

    save_s3(draft, key, meta)
    return {"saved": f"s3://{S3_OUT}/{key}", "meta": meta}

# --- (Opcional) Healthcheck ---
@app.get("/health")
def health():
    return {
        "status": "ok",
        "region": REGION,
        "model_id": MODEL_ID,
        "kendra_index_set": bool(KENDRA_INDEX_ID),
        "s3_out": S3_OUT
    }

# --- (Opcional) Execução local: uvicorn app:app --reload --port 8080 ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=True)
