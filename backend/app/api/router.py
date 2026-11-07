from fastapi import APIRouter

from app.api import auth, documents, health, search, structure, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(structure.router)
api_router.include_router(documents.router)
api_router.include_router(search.router)
