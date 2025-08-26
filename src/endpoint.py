from flask import Flask, request, jsonify
import os, traceback, boto3
from botocore.exceptions import BotoCoreError, ClientError


REGION = os.getenv("AWS_REGION", "us-east-1")
KB_ID  = os.getenv("KNOWLEDGE_BASE_ID")
MODEL  = os.getenv("MODEL_ARN")

SYSTEM_INSTRUCTIONS = (
    "Você é uma IA que responde em português, de forma objetiva, "
    "usando EXCLUSIVAMENTE as informações recuperadas da Knowledge Base.\n"
    "Se as fontes recuperadas não contiverem evidências suficientes, responda exatamente: "
    "\"Não encontrei informações sobre isso na base de conhecimento.\"\n"
    "Inclua ao final uma seção 'Fontes' listando as referências (URI) quando houver."
)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False  

def kb_client():
    return boto3.client("bedrock-agent-runtime", region_name=REGION)

def missing_env():
    miss = []
    if not REGION: miss.append("AWS_REGION")
    if not KB_ID:  miss.append("KNOWLEDGE_BASE_ID")
    if not MODEL:  miss.append("MODEL_ARN")
    return miss

def add_id_header(resp):
    resp.headers["X-Service"] = "flask-kb"
    return resp

@app.get("/ping")
def ping():
    return jsonify({"ok": True})

@app.get("/_diag")
def diag():
    env = {"AWS_REGION": REGION, "KNOWLEDGE_BASE_ID": KB_ID, "MODEL_ARN": MODEL}
    miss = missing_env()
    if miss:
        return jsonify({"ok": False, "error": "Missing env vars", "missing": miss, "env": env}), 500
    try:
        
        r = kb_client().retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": "ping"},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 1}},
        )
        
        prompt = f"{SYSTEM_INSTRUCTIONS}\n\nPergunta: Diga 'ok' se você está funcionando."
        rag = kb_client().retrieve_and_generate(
            input={"text": prompt},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": KB_ID,
                    "modelArn": MODEL,
                }
            }
        )
        preview = (rag.get("output", {}).get("text", "") or "")[:120]
        return jsonify({
            "ok": True,
            "retrieve_count": len(r.get("retrievalResults", [])),
            "rag_preview": preview
        }), 200
    except (BotoCoreError, ClientError) as e:
        return jsonify({"ok": False, "aws_error": str(e), "env": env}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc(), "env": env}), 500


@app.post("/chat")
def chat():
    try:
        miss = missing_env()
        if miss:
            return jsonify({"error": "Missing env vars", "missing": miss}), 500

        data = request.get_json(force=True, silent=False) or {}
        user_msg = (data.get("message") or "").strip()
        if not user_msg:
            return jsonify({"error": "Body precisa conter JSON com o campo 'message'."}), 400

    
        final_prompt = f"{SYSTEM_INSTRUCTIONS}\n\nPergunta do usuário: {user_msg}"

        resp = kb_client().retrieve_and_generate(
            input={"text": final_prompt},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": KB_ID,
                    "modelArn": MODEL
                }
            }
        )

        answer = resp.get("output", {}).get("text", "") or ""
        citations = []
        for c in resp.get("citations", []):
            for ref in c.get("retrievedReferences", []):
                citations.append({
                    "uri": ref.get("metadata", {}).get("x-amz-bedrock-kb-source-uri"),
                    "score": ref.get("score")
                })


        if not citations and "Não encontrei informações" not in answer:
            answer = "Não encontrei informações sobre isso na base de conhecimento."

        return jsonify({"answer": answer, "citations": citations}), 200

    except (BotoCoreError, ClientError) as e:
        return jsonify({"error": "AWS error", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":

    app.run(host="0.0.0.0", port=8081)
