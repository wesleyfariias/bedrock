# app-no-s3.py — FastAPI sem fluxo de aprovação/S3
# Gera documentos (User Story, RTR) e devolve o markdown diretamente

import os
import json
import re
import datetime as dt
from typing import Optional, List, Dict, Literal

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from botocore.exceptions import ClientError

# --- Config ---
REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("MODEL_ID", "anthropic.claude-v2:1")  # Claude 2.1
KENDRA_INDEX_ID = os.getenv("KENDRA_INDEX_ID")  # opcional
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# --- AWS clients ---
s3 = boto3.client("s3", region_name=REGION)  # não usamos diretamente, mas mantém compat p/ futuro
kendra = boto3.client("kendra", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# --- FastAPI ---
app = FastAPI(title="PMESP AI Orchestrator (Kendra + Claude 2.1) — Sem S3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Helpers ---
def kendra_search(query: str, top_k: int = 8) -> List[Dict[str, str]]:
    if not KENDRA_INDEX_ID:
        return []
    try:
        r = kendra.query(IndexId=KENDRA_INDEX_ID, QueryText=query, PageSize=top_k)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "ValidationException"):
            print(f"[KENDRA] fallback sem contexto: {code} - {e}")
            return []
        raise
    chunks = []
    for item in r.get("ResultItems", []):
        if item.get("Type") in ("ANSWER", "DOCUMENT"):
            title = item.get("DocumentTitle", {}).get("Text", "")
            text = item.get("DocumentExcerpt", {}).get("Text", "")
            src = item.get("DocumentId", "")
            chunks.append({"title": title, "text": text, "source": src})
    return chunks


def bedrock_claude21(system: str, user_prompt: str, max_tokens: int = 2000, temperature: float = 0.2) -> str:
    prompt = f"\n\nHuman: {system}\n\n{user_prompt}\n\nAssistant:"
    body = {
        "prompt": prompt,
        "max_tokens_to_sample": max_tokens,
        "temperature": temperature,
        "stop_sequences": ["\n\nHuman:"],
    }
    try:
        resp = bedrock.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
        out = json.loads(resp["body"].read())
        return out.get("completion", "").strip()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bedrock invoke error: {e}")


SYSTEM = (
    "Você é o Assistente de Engenharia da PMESP.\n"
    "- Siga estritamente os templates oficiais e processos do contexto.\n"
    "- Se faltar dado, faça perguntas objetivas.\n"
    "- Ao final, acrescente seção 'Fontes' com os paths do contexto (quando houver).\n"
    "- Responda em português claro e direto."
)

# --- Schemas ---
class ChatMsg(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatIn(BaseModel):
    messages: List[ChatMsg]


class GenIn(BaseModel):
    objetivo: str
    contexto: Optional[str] = None


# --- Endpoints ---
@app.get("/health")
def health():
    return {
        "status": "ok",
        "region": REGION,
        "model_id": MODEL_ID,
        "kendra_index_set": bool(KENDRA_INDEX_ID),
    }


@app.post("/chat")
def chat(inp: ChatIn):
    # última mensagem do usuário vira a query para o Kendra
    user_query = next((m.content for m in reversed(inp.messages) if m.role == "user"), "")
    context_hits = kendra_search(user_query, top_k=6) if user_query else []
    ctx_text = (
        "\n\n".join(f"# {c['title']}\n{c['text']}\n(Fonte:{c['source']})" for c in context_hits)
        if context_hits
        else "(Sem contexto do Kendra disponível)"
    )

    transcript = "\n".join(
        [f"Usuário: {m.content}" if m.role == "user" else f"Assistente: {m.content}" for m in inp.messages]
    )

    user_prompt = (
        "Contexto do Kendra (quando houver, use para embasar a resposta e cite as fontes ao final):\n"
        f"{ctx_text}\n\n"
        "Conversa até aqui (responda ao último pedido do usuário de forma objetiva e técnica):\n"
        f"{transcript}\n\n"
        "Requisitos:\n"
        "- Cite 'Fontes' ao final quando houver contexto do Kendra.\n"
        "- Se faltar informação, faça perguntas diretas e sucintas.\n"
        "- Não invente políticas internas; respeite padrões da PMESP quando aparecerem no contexto."
    )

    completion = bedrock_claude21(system=SYSTEM, user_prompt=user_prompt, max_tokens=1500, temperature=0.2)

    return {"answer": completion, "kendra_sources": [c["source"] for c in context_hits]}


@app.post("/gen/user-story")
def gen_user_story(inp: GenIn):
    k_query = f"template user story padrão PMESP; {inp.objetivo} {inp.contexto or ''}"
    context = kendra_search(k_query, top_k=8)
    ctx_text = (
        "\n\n".join(f"# {c['title']}\n{c['text']}\n(Fonte:{c['source']})" for c in context)
        if context
        else "(Sem contexto do Kendra disponível)"
    )

    prompt = (
        "Contexto do Kendra (use como referência e cite as fontes ao final):\n"
        f"{ctx_text}\n\n"
        "Gere uma **USER STORY** no padrão oficial (template), contendo:\n"
        "- Título\n- Descrição (HTML) autoexplicativa\n- Critérios de aceite em BDD (Given/When/Then)\n- Dependências e Riscos\n- Definição de Pronto (DoD)\n"
        "Se faltar dado, inclua seção 'Perguntas' com dúvidas objetivas.\n"
        "Inclua ao final uma seção 'Fontes' com os paths do contexto."
    )

    out = bedrock_claude21(system=SYSTEM, user_prompt=prompt, max_tokens=2000, temperature=0.2)
    return {"content": out, "kendra_sources": [c["source"] for c in context]}


@app.post("/gen/rtr")
def gen_rtr(inp: GenIn):
    k_query = f"template RTR padrão PMESP; {inp.objetivo} {inp.contexto or ''}"
    context = kendra_search(k_query, top_k=8)
    ctx_text = (
        "\n\n".join(f"# {c['title']}\n{c['text']}\n(Fonte:{c['source']})" for c in context)
        if context
        else "(Sem contexto do Kendra disponível)"
    )

    prompt = (
        "Contexto do Kendra (use como referência e cite as fontes ao final):\n"
        f"{ctx_text}\n\n"
        "Gere um **RTR** no padrão oficial (template), contendo ao menos:\n"
        "- Identificação (projeto/sistema, versão, data, responsáveis)\n"
        "- Objetivo do RTR\n- Escopo e Não Escopo\n- Arquitetura/Design resumido (ou referências)\n"
        "- Dependências, Riscos e Premissas\n- Critérios de aceite / validação\n- Anexos e Links\n"
        "Se faltar dado, inclua uma seção 'Perguntas' com dúvidas objetivas.\n"
        "Inclua ao final uma seção 'Fontes' com os paths do contexto."
    )

    out = bedrock_claude21(system=SYSTEM, user_prompt=prompt, max_tokens=2000, temperature=0.2)
    return {"content": out, "kendra_sources": [c["source"] for c in context]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app-no-s3:app", host="0.0.0.0", port=int(os.getenv("PORT", "8081")), reload=True)
