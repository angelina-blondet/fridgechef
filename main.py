
import asyncio, base64, json, os, time, unicodedata
from collections import defaultdict

import httpx
import jwt
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"]
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")
_jwks = jwt.PyJWKClient(
    f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
)

PEXELS_KEY = os.getenv("PEXELS_API_KEY")
DAILY_LIMIT = 10
MAX_IMAGE = 5 * 1024 * 1024

PANTRY = set()
_usage = defaultdict(list)


def verify(token: str) -> dict:
    if jwt.get_unverified_header(token).get("alg") == "HS256":
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated"
        )

    key = _jwks.get_signing_key_from_jwt(token).key

    return jwt.decode(
        token,
        key,
        algorithms=["ES256", "RS256"],
        audience="authenticated"
    )


def current_user(authorization: str = Header(...)) -> str:
    try:
        token = authorization.removeprefix("Bearer ").strip()
        payload = verify(token)
    except Exception:
        raise HTTPException(401, "Non authentifié")

    uid, now = payload["sub"], time.time()

    _usage[uid] = [
        t for t in _usage[uid]
        if now - t < 86400
    ]

    if len(_usage[uid]) >= DAILY_LIMIT:
        raise HTTPException(429, "Limite quotidienne atteinte")

    _usage[uid].append(now)

    return uid


def parse_json(text: str):
    text = (
        text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(502, "Réponse IA invalide")


async def ask(content, system: str, max_tokens: int = 2000) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(
            503,
            "Le service IA n'est pas configuré (GEMINI_API_KEY manquante)."
        )

    parts = []

    for item in content if isinstance(content, list) else [
        {"type": "text", "text": content}
    ]:
        if item.get("type") == "image":
            parts.append({
                "inlineData": {
                    "mimeType": item["source"]["media_type"],
                    "data": item["source"]["data"],
                }
            })
        else:
            parts.append({
                "text": item.get("text", "")
            })

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{MODEL}:generateContent"
    )

    payload = {
        "systemInstruction": {
            "parts": [{"text": system}]
        },
        "contents": [{
            "role": "user",
            "parts": parts
        }],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=60) as h:
            response = await h.post(
                url,
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json=payload
            )

        if response.status_code == 429:
            raise HTTPException(
                429,
                "Limite gratuite de l'IA atteinte. Réessaie plus tard."
            )

        response.raise_for_status()
        data = response.json()

        candidates = data.get("candidates", [])

        if not candidates:
            raise HTTPException(
                502,
                "Le service IA n'a pas renvoyé de réponse exploitable."
            )

        return "".join(
            part.get("text", "")
            for part in candidates[0].get("content", {}).get("parts", [])
            if part.get("text")
        )

    except HTTPException:
        raise

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502,
            f"Erreur du service IA (HTTP {exc.response.status_code})."
        )

    except httpx.HTTPError:
        raise HTTPException(
            502,
            "Impossible de contacter le service IA."
        )


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())

    return "".join(
        c for c in s
        if unicodedata.category(c) != "Mn"
    ).strip()


def allowed(name: str, fridge: list[str]) -> bool:
    return norm(name) in fridge


async def photo(query: str):
    if not PEXELS_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=6) as h:
            r = await h.get(
                "https://api.pexels.com/v1/search",
                params={
                    "query": f"{query} dish",
                    "per_page": 1,
                    "orientation": "landscape"
                },
                headers={"Authorization": PEXELS_KEY}
            )

            photos = r.json().get("photos", [])

            return photos[0]["src"]["large"] if photos else None

    except Exception:
        return None


@app.post("/scan")
async def scan(
    image: UploadFile = File(...),
    _: str = Depends(current_user)
):
    if image.content_type not in ("image/jpeg", "image/png"):
        raise HTTPException(415, "JPEG ou PNG uniquement")

    data = await image.read()

    if len(data) > MAX_IMAGE:
        raise HTTPException(413, "Image trop lourde")

    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.content_type,
                "data": base64.b64encode(data).decode()
            }
        },
        {
            "type": "text",
            "text": "Liste les aliments visibles dans ce frigo."
        },
    ]

    system = (
        'Réponds UNIQUEMENT par un JSON : '
        '{"ingredients": ["aliment1", ...]}. '
        "Noms courts en français, sans doublons, "
        "uniquement des aliments comestibles."
    )

    out = parse_json(await ask(content, system))

    return {
        "ingredients": [
            str(x)[:50]
            for x in out.get("ingredients", [])
        ][:60]
    }


class RecipeRequest(BaseModel):
    ingredients: list[str] = Field(max_length=60)
    servings: int = Field(default=2, ge=1, le=6)


@app.post("/recipes")
async def recipes(
    req: RecipeRequest,
    _: str = Depends(current_user)
):
    fridge = [norm(i) for i in req.ingredients]

    system = (
        'Réponds UNIQUEMENT par un JSON : '
        '{"recipes": [{"title": str, '
        '"image_query": str, "time_minutes": int, '
        '"servings": int, '
        '"ingredients": [{"name": str, "quantity": str}], '
        '"steps": [str]}]}. '

        "OBJECTIF : proposer de véritables recettes de cuisine, "
        "qui permettent de préparer un plat réel et appétissant. "

        "Chaque recette doit obligatoirement utiliser au moins "
        "2 ingrédients différents présents dans la liste du frigo. "

        "INTERDICTION de proposer des boissons, des verres de lait, "
        "des aliments simplement servis, des aliments seuls, "
        "ou des préparations qui ne constituent pas un vrai plat. "

        "Ne propose jamais un verre de lait, un bol de lait, "
        "un aliment simplement coupé ou une recette dont le seul "
        "but est de consommer un ingrédient tel quel. "

        "Tu dois utiliser exclusivement les ingrédients "
        "de la liste fournie. "

        "Recopie exactement leurs noms dans les ingrédients "
        "des recettes. "

        "Aucun ingrédient supplémentaire n'est autorisé, "
        "même pas le sel, le poivre, l'eau, l'huile, le beurre, "
        "le lait ou un ingrédient habituellement présent "
        "dans une cuisine, sauf s'il figure dans la liste. "

        "Les quantités doivent correspondre aux ingrédients "
        "réellement disponibles et au nombre de personnes. "

        "Les étapes doivent expliquer une véritable préparation "
        "culinaire avec les ingrédients disponibles. "

        "Privilégie les plats principaux, les plats chauds, "
        "les plats cuisinés et les préparations complètes. "

        "Ne crée pas de recette artificielle simplement pour "
        "atteindre le nombre maximum de recettes. "

        "Si les ingrédients disponibles ne permettent pas "
        "de préparer un véritable plat avec au moins "
        "2 ingrédients différents, retourne une liste vide. "

        f"Chaque recette doit être prévue pour exactement "
        f"{req.servings} personne(s). "

        "Adapte toutes les quantités au nombre de personnes demandé. "

        f"Indique toujours servings à {req.servings} dans chaque recette. "

        "Propose jusqu'à 4 recettes différentes et réalisables. "

        "Retourne une liste vide uniquement si aucun véritable "
        "plat ne peut être préparé avec les ingrédients disponibles."
    )

    msg = (
        "Ingrédients du frigo : "
        + ", ".join(i[:50] for i in req.ingredients)
        + f". Nombre de personnes : {req.servings}."
        + " Je veux de vrais plats cuisinés, pas des boissons "
        "ni des aliments simplement servis."
    )

    out = parse_json(
        await ask(msg, system, max_tokens=3500)
    )

    good = []

    for r in out.get("recipes", []):
        try:
            ingredients = r["ingredients"]
            steps = r["steps"]

            # Vérification des ingrédients
            names = [
                norm(i["name"])
                for i in ingredients
            ]

            # Au moins deux ingrédients différents
            distinct = set(names)

            if len(distinct) < 2:
                continue

            # Tous les ingrédients doivent être dans le frigo
            if not all(
                allowed(i["name"], fridge)
                for i in ingredients
            ):
                continue

            # Une recette doit avoir des étapes
            if not isinstance(steps, list) or not steps:
                continue

            if not all(isinstance(step, str) and step.strip() for step in steps):
                continue

            r["servings"] = req.servings
            good.append(r)

        except (KeyError, TypeError):
            continue

    good = good[:4]

    urls = await asyncio.gather(
        *(
            photo(r.get("image_query") or r["title"])
            for r in good
        )
    )

    for r, u in zip(good, urls):
        r["image_url"] = u
        r.pop("image_query", None)

    return {"recipes": good}
