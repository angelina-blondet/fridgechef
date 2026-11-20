import asyncio, base64, json, os, time, unicodedata
from collections import defaultdict

import httpx
import jwt
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI()
# Autorise ta PWA à appeler l'API. En prod : ALLOWED_ORIGINS=https://ton-site.netlify.app
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # secret serveur uniquement
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")  # seulement pour les anciens projets Supabase
_jwks = jwt.PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")


def verify(token: str) -> dict:
    if jwt.get_unverified_header(token).get("alg") == "HS256":  # ancien système
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience="authenticated")
    key = _jwks.get_signing_key_from_jwt(token).key  # nouveaux projets (clés asymétriques)
    return jwt.decode(token, key, algorithms=["ES256", "RS256"], audience="authenticated")
PEXELS_KEY = os.getenv("PEXELS_API_KEY")  # photos des plats (gratuit : pexels.com/api)
DAILY_LIMIT = 10
MAX_IMAGE = 5 * 1024 * 1024
# Seuls ingrédients autorisés EN PLUS de ceux du frigo. Retire-les ou ajoute "huile" si tu veux.
PANTRY = {"sel", "poivre", "eau"}
_usage = defaultdict(list)  # MVP en mémoire. En prod : Redis/Postgres.


def current_user(authorization: str = Header(...)) -> str:
    try:
        token = authorization.removeprefix("Bearer ").strip()
        payload = verify(token)
    except Exception:
        raise HTTPException(401, "Non authentifié")
    uid, now = payload["sub"], time.time()
    _usage[uid] = [t for t in _usage[uid] if now - t < 86400]
    if len(_usage[uid]) >= DAILY_LIMIT:
        raise HTTPException(429, "Limite quotidienne atteinte")
    _usage[uid].append(now)
    return uid


def parse_json(text: str):
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(502, "Réponse IA invalide")


async def ask(content, system: str, max_tokens: int = 2000) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(503, "Le service IA n'est pas configuré (GEMINI_API_KEY manquante).")

    # Gemini attend des parties texte et, pour le scan, une image inline en base64.
    parts = []
    for item in content if isinstance(content, list) else [{"type": "text", "text": content}]:
        if item.get("type") == "image":
            parts.append({"inlineData": {
                "mimeType": item["source"]["media_type"],
                "data": item["source"]["data"],
            }})
        else:
            parts.append({"text": item.get("text", "")})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens, "responseMimeType": "application/json"},
    }
    try:
        async with httpx.AsyncClient(timeout=60) as h:
            response = await h.post(
                url, headers={"x-goog-api-key": GEMINI_API_KEY}, json=payload
            )
        if response.status_code == 429:
            raise HTTPException(429, "Limite gratuite de l'IA atteinte. Réessaie plus tard.")
        response.raise_for_status()
        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise HTTPException(502, "Le service IA n'a pas renvoyé de réponse exploitable.")
        return "".join(
            part.get("text", "")
            for part in candidates[0].get("content", {}).get("parts", [])
            if part.get("text")
        )
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        # Ne pas renvoyer le corps brut : il pourrait contenir des détails techniques.
        raise HTTPException(502, f"Erreur du service IA (HTTP {exc.response.status_code}).")
    except httpx.HTTPError:
        raise HTTPException(502, "Impossible de contacter le service IA.")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def allowed(name: str, fridge: list[str]) -> bool:
    n = norm(name)
    if n in PANTRY:
        return True
    return len(n) >= 3 and any(i and (i in n or n in i) for i in fridge)


async def photo(query: str):
    if not PEXELS_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=6) as h:
            r = await h.get("https://api.pexels.com/v1/search",
                            params={"query": f"{query} dish", "per_page": 1, "orientation": "landscape"},
                            headers={"Authorization": PEXELS_KEY})
            photos = r.json().get("photos", [])
            return photos[0]["src"]["large"] if photos else None
    except Exception:
        return None


@app.post("/scan")
async def scan(image: UploadFile = File(...), _: str = Depends(current_user)):
    if image.content_type not in ("image/jpeg", "image/png"):
        raise HTTPException(415, "JPEG ou PNG uniquement")
    data = await image.read()
    if len(data) > MAX_IMAGE:
        raise HTTPException(413, "Image trop lourde")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": image.content_type,
                                      "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": "Liste les aliments visibles dans ce frigo."},
    ]
    system = ('Réponds UNIQUEMENT par un JSON : {"ingredients": ["aliment1", ...]}. '
              "Noms courts en français, sans doublons, uniquement des aliments comestibles.")
    out = parse_json(await ask(content, system))
    return {"ingredients": [str(x)[:50] for x in out.get("ingredients", [])][:60]}


class RecipeRequest(BaseModel):
    ingredients: list[str] = Field(max_length=60)


@app.post("/recipes")
async def recipes(req: RecipeRequest, _: str = Depends(current_user)):
    fridge = [norm(i) for i in req.ingredients]
    system = (
        'Réponds UNIQUEMENT par un JSON : {"recipes": [{"title": str (français), '
        '"image_query": str (nom du plat en anglais, ex: "zucchini gratin"), "time_minutes": int, '
        '"servings": int, "ingredients": [{"name": str, "quantity": str}], "steps": [str]}]}. '
        "RÈGLE ABSOLUE : n'utilise QUE les ingrédients de la liste fournie, avec leur nom exact. "
        "Seuls ajouts permis : sel, poivre, eau. Aucun autre ingrédient, jamais. "
        "Propose jusqu'à 4 plats réalisables ; moins si c'est impossible, liste vide si aucun."
    )
    msg = "Ingrédients du frigo : " + ", ".join(i[:50] for i in req.ingredients)
    out = parse_json(await ask(msg, system, max_tokens=3500))

    good = []
    for r in out.get("recipes", []):
        try:
            if r["steps"] and all(allowed(i["name"], fridge) for i in r["ingredients"]):
                good.append(r)
        except (KeyError, TypeError):
            continue
    good = good[:4]
    urls = await asyncio.gather(*(photo(r.get("image_query") or r["title"]) for r in good))
    for r, u in zip(good, urls):
        r["image_url"] = u
        r.pop("image_query", None)
    return {"recipes": good}
