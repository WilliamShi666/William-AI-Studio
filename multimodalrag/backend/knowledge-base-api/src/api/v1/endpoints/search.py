from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List

router = APIRouter()

class SearchRequest(BaseModel):
    query: str
    top_k: int

class SearchResponse(BaseModel):
    id: str
    filename: str
    score: float

@router.post("/search", response_model=List[SearchResponse])
async def search_vectors(request: SearchRequest):
    raise HTTPException(
        status_code=501,
        detail="Legacy vector search is not wired in the public optional knowledge-base API. Use the main Roys Legion Milvus API for public RAG flows.",
    )
