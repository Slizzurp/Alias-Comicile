# Alias-Comicile-v.2.py
import os, uuid, math, datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, DateTime, Float, Integer, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.sql import func
from cryptography.fernet import Fernet
from passlib.context import CryptContext
from jose import jwt
from dotenv import load_dotenv

# --- Environment Setup ---
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
FERNET_KEY = os.getenv("FERNET_KEY")
JWT_SECRET = os.getenv("JWT_SECRET", "your_default_jwt_secret")
ALGORITHM = "HS256"

# --- App Initialization ---
app = FastAPI(title="Alias-Comicile v2.0")
Base = declarative_base()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
if not FERNET_KEY:
    # For development, generate a new key if not set; in production, require it
    FERNET_KEY = Fernet.generate_key().decode()
fernet = Fernet(FERNET_KEY.encode())
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- Models ---
class Artist(Base):
    __tablename__ = "artists"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String)
    email = Column(String, unique=True)
    password = Column(String)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

class ArtPiece(Base):
    __tablename__ = "art_pieces"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    artist_id = Column(String, ForeignKey("artists.id"))
    title = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    base_value = Column(Float, default=1.0)
    encrypted_value = Column(String)
    meta_data = Column(String)

class SocialMetric(Base):
    __tablename__ = "social_metrics"
    art_piece_id = Column(String, ForeignKey("art_pieces.id"), primary_key=True)
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    competition_score = Column(Float, default=0.0)
    last_updated = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Valuation(Base):
    __tablename__ = "valuations"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    art_piece_id = Column(String, ForeignKey("art_pieces.id"))
    computed_value = Column(Float)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

Base.metadata.create_all(bind=engine)

# --- Utility Functions ---
def hash_password(password): return pwd_context.hash(password)
def verify_password(plain, hashed): return pwd_context.verify(plain, hashed)
def create_token(data): return jwt.encode(data, JWT_SECRET, algorithm=ALGORITHM)
def decode_token(token): return jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])

ALPHA, BETA, GAMMA, LAMBDA = 0.4, 0.3, 0.3, 0.01
def compute_value(piece, metrics):
    age_days = (datetime.datetime.now(datetime.timezone.utc) - piece.created_at).days
    D = math.exp(-LAMBDA * age_days)
    val = piece.base_value
    val *= (1 + ALPHA * math.log1p(metrics.views))
    val *= (1 + BETA * math.sqrt(metrics.likes))
    val *= (1 + GAMMA * metrics.competition_score / 100)
    return val * D

def record_and_encrypt(art_id):
    db = SessionLocal()
    piece = db.query(ArtPiece).get(art_id)
    metrics = db.query(SocialMetric).get(art_id)
    if piece is None or metrics is None:
        db.close()
        return
    val = compute_value(piece, metrics)
    piece.encrypted_value = fernet.encrypt(f"{val:.4f}".encode()).decode()
    db.add(Valuation(art_piece_id=art_id, computed_value=val))
    db.commit()
    db.close()

# --- Schemas ---
class ArtistAuth(BaseModel):
    name: str
    email: str
    password: str

class RegisterIn(BaseModel):
    artist_id: str
    title: str
    base_value: float = 1.0
    metadata: dict

class MetricsIn(BaseModel):
    art_piece_id: str
    views: int
    likes: int
    competition_score: float

# --- API Endpoints ---
@app.post("/artist/signup")
def signup(data: ArtistAuth):
    db = SessionLocal()
    artist = Artist(name=data.name, email=data.email, password=hash_password(data.password))
    db.add(artist)
    db.commit()
    db.refresh(artist)
    db.close()
    return {"artist_id": artist.id}

@app.post("/token")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    db = SessionLocal()
    artist = db.query(Artist).filter(Artist.email == form_data.username).first()
    db.close()
    if not artist or not verify_password(form_data.password, artist.password):
        raise HTTPException(401, "Invalid credentials")
    return {"access_token": create_token({"sub": artist.id}), "token_type": "bearer"}

@app.post("/art/register")
def register_art(data: RegisterIn, token: str = Depends(oauth2_scheme)):
    decode_token(token)
    db = SessionLocal()
    piece = ArtPiece(
        artist_id=data.artist_id,
        title=data.title,
        base_value=data.base_value,
        meta_data=str(data.metadata)
    )
    db.add(piece)
    db.add(SocialMetric(art_piece_id=piece.id))
    db.commit()
    db.refresh(piece)
    db.close()
    return {"art_piece_id": piece.id}

@app.post("/metrics/update")
def update_metrics(data: MetricsIn, bg: BackgroundTasks):
    db = SessionLocal()
    metrics = db.query(SocialMetric).get(data.art_piece_id)
    if metrics is None:
        metrics = SocialMetric(
            art_piece_id=data.art_piece_id,
            views=data.views,
            likes=data.likes,
            competition_score=data.competition_score
        )
        db.add(metrics)
    else:
        metrics.views += data.views
        metrics.likes += data.likes
        metrics.competition_score = max(metrics.competition_score, data.competition_score)
    db.commit()
    db.close()
    bg.add_task(record_and_encrypt, data.art_piece_id)
    return {"status": "updated"}

@app.get("/value/{art_piece_id}")
def get_value(art_piece_id: str, token: str = Depends(oauth2_scheme)):
    decode_token(token)
    db = SessionLocal()
    piece = db.query(ArtPiece).get(art_piece_id)
    if piece is None or piece.encrypted_value is None:
        db.close()
        raise HTTPException(status_code=404, detail="Art piece not found or value not available")
    val = float(fernet.decrypt(piece.encrypted_value.encode()).decode())
    db.close()
    return {"value": val}

@app.get("/history/{art_piece_id}")
def get_history(art_piece_id: str, token: str = Depends(oauth2_scheme)):
    decode_token(token)
    db = SessionLocal()
    history = db.query(Valuation).filter_by(art_piece_id=art_piece_id).all()
    db.close()
    return [{"value": v.computed_value, "timestamp": v.timestamp} for v in history]