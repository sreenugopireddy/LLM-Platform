"""
LLM Platform — Prompt Registry Service

Treats prompts as versioned software:
  POST   /prompts/{name}/versions                  create draft
  GET    /prompts/{name}/versions                  list all
  GET    /prompts/{name}/versions/{ver}            get one
  POST   /prompts/{name}/versions/{ver}/promote    promote (eval-gated)
  PATCH  /prompts/{name}/versions/{ver}/eval-scores write scores
  GET    /prompts/{name}/production                active production version
  DELETE /prompts/{name}/versions/{ver}            delete draft
"""
import logging, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse

from shared.cosmos_client import CosmosClient
from shared.prompt_schema import PromptVersionCreate, PromptVersionDoc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("prompt-registry")

cosmos = CosmosClient("prompts")
EVAL_GATE_THRESHOLD = 0.85


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Prompt registry started")
    yield
    await cosmos.close()


app = FastAPI(title="LLM Platform Prompt Registry", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "prompt-registry"}


@app.post("/prompts/{name}/versions", status_code=201)
async def create_version(name: str, body: PromptVersionCreate):
    doc_id = f"{name}:{body.version}"
    if await cosmos.get(doc_id):
        raise HTTPException(409, detail={"error": "version_exists", "hint": "bump the version number"})
    doc = PromptVersionDoc(id=doc_id, name=name, version=body.version,
                           template=body.template, input_schema=body.input_schema,
                           description=body.description)
    saved = await cosmos.upsert(doc.model_dump())
    logger.info("Created %s", doc_id)
    return saved


@app.get("/prompts/{name}/versions")
async def list_versions(name: str):
    all_docs = await cosmos.query("SELECT * FROM c WHERE c.name = @name",
                                  parameters=[{"name": "@name", "value": name}])
    filtered = [d for d in all_docs if d.get("name") == name]
    return {"prompt": name, "versions": filtered, "count": len(filtered)}


@app.get("/prompts/{name}/versions/{version}")
async def get_version(name: str, version: str):
    doc = await cosmos.get(f"{name}:{version}")
    if not doc:
        raise HTTPException(404, detail={"error": "version_not_found"})
    return doc


@app.get("/prompts/{name}/production")
async def get_production(name: str):
    all_docs = await cosmos.query("SELECT * FROM c WHERE c.name = @name AND c.status = 'production'",
                                  parameters=[{"name": "@name", "value": name}])
    prod = [d for d in all_docs if d.get("name") == name and d.get("status") == "production"]
    if not prod:
        raise HTTPException(404, detail={"error": "no_production_version"})
    prod.sort(key=lambda d: d.get("promoted_at") or "", reverse=True)
    return prod[0]


@app.post("/prompts/{name}/versions/{version}/promote")
async def promote_version(name: str, version: str, x_promoted_by: str = Header(default="system")):
    doc = await cosmos.get(f"{name}:{version}")
    if not doc:
        raise HTTPException(404, detail={"error": "version_not_found"})
    prompt = PromptVersionDoc(**doc)

    if not prompt.eval_scores:
        raise HTTPException(409, detail={"error": "eval_scores_missing",
                                         "hint": "run evals first: PATCH eval-scores"})
    failing = {k: v for k, v in prompt.eval_scores.items() if v < EVAL_GATE_THRESHOLD}
    if failing:
        raise HTTPException(409, detail={"error": "eval_gate_failed",
                                         "threshold": EVAL_GATE_THRESHOLD,
                                         "failing_scores": failing})

    # Supersede old production version
    old_docs = await cosmos.query("SELECT * FROM c WHERE c.name = @name AND c.status = 'production'",
                                  parameters=[{"name": "@name", "value": name}])
    for old in [d for d in old_docs if d.get("name") == name and d.get("status") == "production"]:
        old["status"] = "superseded"
        await cosmos.upsert(old)

    doc["status"] = "production"
    doc["promoted_at"] = datetime.now(timezone.utc).isoformat()
    doc["promoted_by"] = x_promoted_by
    await cosmos.upsert(doc)
    logger.info("Promoted %s:%s → production", name, version)
    return {"promoted": True, "prompt": name, "version": version, "eval_scores": prompt.eval_scores}


@app.patch("/prompts/{name}/versions/{version}/eval-scores")
async def write_eval_scores(name: str, version: str, scores: dict):
    doc = await cosmos.get(f"{name}:{version}")
    if not doc:
        raise HTTPException(404, detail={"error": "version_not_found"})
    doc["eval_scores"] = scores
    await cosmos.upsert(doc)
    return {"prompt": name, "version": version, "eval_scores": scores}


@app.delete("/prompts/{name}/versions/{version}", status_code=204)
async def delete_version(name: str, version: str):
    doc = await cosmos.get(f"{name}:{version}")
    if not doc:
        raise HTTPException(404, detail={"error": "version_not_found"})
    if doc.get("status") == "production":
        raise HTTPException(409, detail={"error": "cannot_delete_production_version"})
    await cosmos.delete(f"{name}:{version}")
    return JSONResponse(status_code=204, content=None)