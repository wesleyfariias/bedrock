import json, os, boto3

# Modelo de TEXTO (Claude v2)
MODEL_ID = os.environ.get("MODEL_ID", "anthropic.claude-v2")
REGION   = os.environ.get("BEDROCK_REGION", "us-east-1")

brt = boto3.client("bedrock-runtime", region_name=REGION)

def handler(event, context):
    # Lê a mensagem do corpo
    try:
        body = json.loads(event.get("body") or "{}")
        user_msg = body.get("message") or "Olá! Resuma como você funciona."
    except Exception:
        user_msg = "Olá! Resuma como você funciona."

    # Formato do Claude v2 (prompt/completion)
    prompt = f"\n\nHuman: {user_msg}\n\nAssistant:"
    payload = {
        "prompt": prompt,
        "max_tokens_to_sample": 512,
        "temperature": 0.7,
        "stop_sequences": ["\n\nHuman:"]
    }

    try:
        resp = brt.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps(payload)
        )
        data = json.loads(resp["body"].read())
        text = data.get("completion", "")

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": text}, ensure_ascii=False)
        }
    except Exception as e:
        # Retorna erro visível para facilitar debug
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}, ensure_ascii=False)
        }