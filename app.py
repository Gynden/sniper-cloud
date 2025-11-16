from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Literal, Optional

# ---------- Tipos básicos ----------

SpinColor = Literal["red", "black", "white"]

app = FastAPI(title="Spectra X - White 5/8 SIMPLES + Auth Base")


class Stats(BaseModel):
    whites_today: int = 0
    losses_today: int = 0
    attempts_today: int = 0
    dist_desde_white: Optional[int] = None  # giros desde o último white (0 = acabou de sair)


class DecisionResponse(BaseModel):
    action: Literal["aguardar", "entrar_white"]
    reason: str
    stats: Stats


class PushRoundPayload(BaseModel):
    number: int  # 0–14 vindo da Blaze


class LoginPayload(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    email: str
    token: str


# "Banco" de usuários em memória (exemplo; depois dá pra ligar em banco de dados)
USERS = {
    "demo@demo.com": {"password": "123456", "token": "DEMO-TOKEN-123"},
    "cliente@seuproduto.com": {"password": "minhasenha", "token": "CLIENTE-TOKEN-XYZ"},
}

CURRENT_STATS = Stats()
LAST_DECISION_WAS_ENTRY: bool = False  # se o giro ANTERIOR era sinal de entrada

security = HTTPBearer()


# ---------- Funções auxiliares ----------

def number_to_color(num: int) -> SpinColor:
    if num == 0:
        return "white"
    if num <= 7:
        return "red"
    return "black"


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    for email, data in USERS.items():
        if data["token"] == token:
            return {"email": email, "token": token}
    raise HTTPException(status_code=401, detail="Token inválido ou expirado")


# ---------- Lógica 5/8 SIMPLES ----------

@app.post("/api/push_round", response_model=DecisionResponse)
def push_round(payload: PushRoundPayload):
    """
    Chamado a cada número novo.
    - Atualiza:
        • resultado da ENTRADA ANTERIOR (se teve)
        • contador de giros desde o último white
    - Decide se o PRÓXIMO giro é entrada 5/8.
    """
    global CURRENT_STATS, LAST_DECISION_WAS_ENTRY

    stats = CURRENT_STATS
    color = number_to_color(payload.number)

    # 1) Atualiza resultado da entrada ANTERIOR
    if LAST_DECISION_WAS_ENTRY:
        stats.attempts_today += 1
        if color == "white":
            stats.whites_today += 1
        else:
            stats.losses_today += 1

    # 2) Atualiza contador de giros desde o último white
    if color == "white":
        # acabou de sair white -> zera
        stats.dist_desde_white = 0
    else:
        if stats.dist_desde_white is not None:
            stats.dist_desde_white += 1

    # 3) Decide se o PRÓXIMO giro é 5º ou 8º após white
    action: Literal["aguardar", "entrar_white"] = "aguardar"
    reason = "Aguardando sair um white."

    if stats.dist_desde_white is not None:
        proximo = stats.dist_desde_white + 1  # o PRÓXIMO giro
        if proximo == 5:
            action = "entrar_white"
            reason = "REGRA 5/8: próximo giro é o 5º após o último white (1ª tentativa)."
        elif proximo == 8:
            action = "entrar_white"
            reason = "REGRA 5/8: próximo giro é o 8º após o último white (2ª tentativa)."
        else:
            reason = (
                f"{stats.dist_desde_white} giros já passaram desde o white; "
                f"próximo será o {proximo}º, aguardando 5º ou 8º."
            )

    LAST_DECISION_WAS_ENTRY = (action == "entrar_white")
    CURRENT_STATS = stats

    return DecisionResponse(
        action=action,
        reason=reason,
        stats=stats,
    )


# ---------- Login básico (pra futura plataforma) ----------

@app.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginPayload):
    user = USERS.get(payload.email)
    if not user or user["password"] != payload.password:
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    return LoginResponse(email=payload.email, token=user["token"])


# ---------- Stats protegidos por token (login) ----------

@app.get("/stats", response_model=Stats)
def get_stats(current_user=Depends(get_current_user)):
    return CURRENT_STATS


@app.post("/reset", response_model=Stats)
def reset_stats(current_user=Depends(get_current_user)):
    global CURRENT_STATS, LAST_DECISION_WAS_ENTRY
    CURRENT_STATS = Stats()
    LAST_DECISION_WAS_ENTRY = False
    return CURRENT_STATS
