# server.py
# FastAPI + MongoDB Atlas backend: CRUD for sculptures (cs3720db.sculptures)
#
# Run (from this backend folder, venv active):
#   uvicorn server:app --host 0.0.0.0 --port 8000 --reload
# Swagger docs: http://localhost:8000/docs
#
# The Atlas connection string is read from the MONGODB_URI environment variable.
# Locally it comes from backend/.env (never committed); on Render it is set in
# the service's Environment settings.

import logging
import os

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, field_validator
from pymongo import MongoClient
from pymongo.errors import PyMongoError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sculptures-api")

load_dotenv()
# Fail fast (5 s) instead of hanging ~30 s when Atlas is unreachable.
client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
db = client["cs3720db"]
collection = db["sculptures"]


# ---------------------------------------------------------------
# App + CORS
# ---------------------------------------------------------------
app = FastAPI(title="Sculptures API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------
# Models
# ---------------------------------------------------------------
class Sculpture(BaseModel):
    name: str
    artist: str
    description: str
    imageUrl: HttpUrl  # must be a valid http:// or https:// URL
    alt: str

    @field_validator("name", "artist", "description", "alt")
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


# ---------------------------------------------------------------
# Error handling: always { "detail": "<message>" }
# ---------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    parts = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"] if p != "body")
        parts.append(f"{field}: {err['msg']}" if field else err["msg"])
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                        content={"detail": "; ".join(parts) or "Invalid request"})


@app.exception_handler(PyMongoError)
async def mongo_handler(request: Request, exc: PyMongoError):
    log.error("Database error: %s", exc)  # log it, never leak it
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        content={"detail": "Database not reachable"})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        content={"detail": "Internal server error"})


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def serialize(doc: dict) -> dict:
    doc["_id"] = str(doc["_id"])  # ObjectId -> str so it can be JSON
    return doc


# ---------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------
@app.get("/hello")
def hello():
    return {"message": "Hello from FastAPI + MongoDB Atlas backend!"}


@app.get("/sculptures", status_code=status.HTTP_200_OK)
def get_sculptures():
    return [serialize(s) for s in collection.find()]


@app.post("/sculptures", status_code=status.HTTP_201_CREATED)
def add_sculpture(sculpture: Sculpture):
    data = sculpture.model_dump(mode="json")  # HttpUrl -> plain str for MongoDB
    if collection.find_one({"name": data["name"]}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="A sculpture with that name already exists")
    result = collection.insert_one(data)
    data["_id"] = str(result.inserted_id)
    return data


@app.put("/sculptures/{name}", status_code=status.HTTP_200_OK)
def update_sculpture(name: str, sculpture: Sculpture):
    existing = collection.find_one({"name": name})
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sculpture not found")

    data = sculpture.model_dump(mode="json")
    # Renaming must not collide with a different sculpture.
    clash = collection.find_one({"name": data["name"], "_id": {"$ne": existing["_id"]}})
    if clash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="A sculpture with that name already exists")

    collection.update_one({"_id": existing["_id"]}, {"$set": data})
    return serialize(collection.find_one({"_id": existing["_id"]}))


@app.delete("/sculptures/{sculpture_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sculpture(sculpture_id: str):
    try:
        oid = ObjectId(sculpture_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid sculpture id")
    result = collection.delete_one({"_id": oid})
    if result.deleted_count == 0:  # also hit on a second delete of the same id
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sculpture not found")
