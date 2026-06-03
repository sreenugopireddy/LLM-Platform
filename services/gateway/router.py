import hashlib
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.security import HTTPBearer

app = FastAPI()
security = HTTPBearer()

EXPERIMENTS = {
    "exp_gpt4o_vs_35": {
        "variants": ["gpt-4o", "gpt-35-turbo"],
        "split": 0.5   # 50/50
    }
}

def ab_bucket(experiment_id: str, user_id: str) -> str:
    """Deterministic sticky assignment — no session storage needed."""
    key = f"{experiment_id}:{user_id}".encode()
    bucket = int(hashlib.md5(key).hexdigest(), 16) / (16 ** 32)
    exp = EXPERIMENTS[experiment_id]
    idx = 0 if bucket < exp["split"] else 1
    return exp["variants"][idx]

@app.post("/v1/chat")
async def chat(request: Request, token=Depends(security)):
    body = await request.json()
    user_id = body.get("user_id")
    experiment_id = body.get("experiment_id", "exp_gpt4o_vs_35")
    
    variant = ab_bucket(experiment_id, user_id)
    # Forward to inference service with chosen model
    body["model"] = variant
    return {"routed_to": variant, **body}