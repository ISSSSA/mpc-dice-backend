from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rest import router as rest_router
from ws import ws_router

app = FastAPI(title="MPC Dice Game Backend (Non-Trusted Server)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rest_router)
app.include_router(ws_router)
