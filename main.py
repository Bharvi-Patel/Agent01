import json
import os
import secrets
import uuid

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from Agent import agent01, review_draft, get_last_tool_result, clean_json_string, VALID_CATEGORIES

load_dotenv(override=True)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

app = FastAPI(title="Content Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory only: holds each session's `messages` conversation history so
# /review can continue where /generate left off. Resets on server restart.
SESSIONS: dict[str, list] = {}

# In-memory auth tokens issued by /login. Resets on server restart, which
# just logs everyone out — fine for an internal single-admin tool.
AUTH_TOKENS: set[str] = set()

bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> None:
    if creds is None or creds.credentials not in AUTH_TOKENS:
        raise HTTPException(status_code=401, detail="Not authenticated")


class LoginRequest(BaseModel):
    username: str
    password: str


class GenerateRequest(BaseModel):
    category: str
    subtopic: str
    word_count: int


class ReviewRequest(BaseModel):
    session_id: str
    decision: str  # "approve" | "reject"
    feedback: str | None = None
    live: bool = False


def _parse_draft(content: str) -> dict:
    try:
        return json.loads(clean_json_string(content))
    except (json.JSONDecodeError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Agent did not return valid draft JSON: {e}. Raw content was: {content!r}",
        )


@app.post("/login")
def login(req: LoginRequest):
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_USERNAME / ADMIN_PASSWORD not set in the server's .env",
        )

    # secrets.compare_digest avoids leaking timing information about
    # how much of the guess was correct.
    valid = secrets.compare_digest(req.username, ADMIN_USERNAME) and secrets.compare_digest(
        req.password, ADMIN_PASSWORD
    )
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = secrets.token_urlsafe(32)
    AUTH_TOKENS.add(token)
    return {"token": token}


@app.post("/generate", dependencies=[Depends(require_auth)])
def generate(req: GenerateRequest):
    if req.category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}",
        )
    if req.word_count < 100:
        raise HTTPException(status_code=400, detail="word_count must be at least 100")

    content, messages = agent01(
        category=req.category, subtopic=req.subtopic, word_count=req.word_count
    )
    draft = _parse_draft(content)

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = messages

    return {"session_id": session_id, "draft": draft}


@app.post("/review", dependencies=[Depends(require_auth)])
def review(req: ReviewRequest):
    messages = SESSIONS.get(req.session_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session_id")

    if req.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    if req.decision == "reject" and not req.feedback:
        raise HTTPException(status_code=400, detail="feedback is required to reject a draft")

    content, messages = review_draft(
        messages, decision=req.decision, feedback=req.feedback, live=req.live
    )
    SESSIONS[req.session_id] = messages

    if req.decision == "reject":
        draft = _parse_draft(content)
        return {"session_id": req.session_id, "draft": draft}

    result = get_last_tool_result(messages, "publish")
    if result is None:
        raise HTTPException(
            status_code=502,
            detail="Approved, but the agent never called publish. Check the conversation.",
        )
    return {"session_id": req.session_id, "result": result}