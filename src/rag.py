
m flask import Flask, request, jsonify
import os, json, boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
KB_ID  = os.environ["KNOWLEDGE_BASE_ID"]
MODEL  = os.environ["MODEL_ARN"]

kb = boto3.client("bedrock-agent-runtime", region_name=REGION)
app = Flask(__name__)

@app.post("/chat")
def chat():
        data = request.get_json(silent=True) or {}
            user_msg = data.get("message") or "Olá!"
                resp = kb.retrieve_and_generate(
                                input={"text": user_msg},
                                        retrieveAndGenerateConfiguration={
                                                        "type": "KNOWLEDGE_BASE",
                                                                    "knowledgeBaseConfiguration": {
                                                                                        "knowledgeBaseId": KB_ID,
                                                                                                        "modelArn": MODEL
                                                                                                                    }
                                                                            }
                                            )
                    answer = resp.get("output", {}).get("text", "")
                        citations = []
                            for c in resp.get("citations", []):
                                        for ref in c.get("retrievedReferences", []):
                                                        citations.append({
                                                                            "uri": ref.get("metadata", {}).get("x-amz-bedrock-kb-source-uri"),
                                                                                            "score": ref.get("score")
                                                                                                        })
                                                            return jsonify({"answer": answer, "citations": citations})

                                                        if __name__ == "__main__":
                                                                app.run(host="0.0.0.0", port=8080)

