from flask import Flask, request, jsonify
import os, re, json, traceback, boto3
from botocore.exceptions import BotoCoreError, ClientError

# ===== Regiões separadas =====
def _env(name, default=""):
    return (os.getenv(name, default) or "").strip()

BEDROCK_REGION = _env("BEDROCK_REGION", "us-east-1")     # Bedrock costuma ficar em us-east-1
KENDRA_REGION  = _env("KENDRA_REGION", _env("AWS_REGION", "sa-east-1"))  # onde está seu índice

KENDRA_INDEX_ID = _env("KENDRA_INDEX_ID")      # obrigatório p/ retrieve
MODEL_ID       = _env("MODEL_ID")              # opcional (on-demand)
MODEL_ARN      = _env("MODEL_ARN")             # opcional (provisioned/profile)

# Fallback on-demand atual (ajuste se quiser outro)
DEFAULT_ON_DEMAND = "anthropic.claude-3-5-sonnet-20241022-v2:0"

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
