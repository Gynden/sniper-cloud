from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Literal, Optional

# ============================
#   CONFIG FASTAPI + CORS
# ============================

app = FastAPI(title="Spectra X - White 5/8 SIMPLES")

# Libera requisições do navegador (extensão / Blaze)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # depois você pode restringir para blaze.com se quiser
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================
#   MODELOS / ESTADO
# ============================

SpinColor = Literal["red", "black", "white"]


class Stats(BaseModel):
    whites_today: int = 0      # quantidade de whites “acertados”
    losses_today: int = 0      # quantas entradas deram loss
    attempts_today: int = 0    # quantas entradas foram feitas
    dist_desde_white: Optional[int] = None  # giros desde o último white (0 = white acabou de sair)
    total_spins: int = 0       # total de resultados recebidos


class DecisionResponse(BaseModel):
    action: Literal["aguardar", "entrar_white"]
    reason: str
    stats: Stats


class PushRoundPayload(BaseModel):
    number: int  # número 0–14 vindo da Blaze


# Estado em memória (simples)
CURRENT_STATS = Stats()
LAST_DECISION_WAS_ENTRY: bool = False  # se o giro ANTERIOR era sinal de entrada


# ============================
#   FUNÇÕES AUXILIARES
# ============================

def number_to_color(num: int) -> SpinColor:
    if num == 0:
        return "white"
    if num <= 7:
        return "red"
    return "black"


# ============================
#   ROTAS
# ============================

@app.get("/")
def root():
    return {"status": "ok", "service": "Spectra X 5/8", "docs": "/docs"}


@app.post("/api/push_round", response_model=DecisionResponse)
def push_round(payload: PushRoundPayload):
    """
    Recebe um número (0–14) da Blaze a cada novo giro.

    1) Atualiza estatísticas:
       - resultado da ENTRADA ANTERIOR (se teve)
       - contador de giros desde o último white

    2) Decide se o PRÓXIMO giro será entrada WHITE
       pela regra 5/8 (5º e 8º giros após o white).
    """
    global CURRENT_STATS, LAST_DECISION_WAS_ENTRY

    stats = CURRENT_STATS
    stats.total_spins += 1

    num = payload.number
    color = number_to_color(num)

    # -------- 1) Resultado da entrada ANTERIOR --------
    if LAST_DECISION_WAS_ENTRY:
        stats.attempts_today += 1
        if color == "white":
            stats.whites_today += 1
        else:
            stats.losses_today += 1

    # -------- 2) Atualiza dist_desde_white --------
    if color == "white":
        # acabou de sair white -> este giro é o próprio white
        stats.dist_desde_white = 0
    else:
        if stats.dist_desde_white is not None:
            # já vimos um white antes -> soma +1 giro
            stats.dist_desde_white += 1
        # se nunca viu white (None), continua None

    # -------- 3) Decide se o PRÓXIMO giro é entrada 5/8 --------
    action: Literal["aguardar", "entrar_white"] = "aguardar"
    reason = "Ainda não saiu white; aguardando primeiro white."

    if stats.dist_desde_white is not None:
        # dist_desde_white = quantos giros JÁ passaram desde o white:
        #  0 -> acabou de sair white
        #  1 -> 1º giro após o white
        #  ...
        proximo_giro = stats.dist_desde_white + 1  # o PRÓXIMO depois deste

        if stats.dist_desde_white == 0:
            reason = "White acabou de sair; próximo será o 1º giro após o white."
        else:
            reason = (
                f"{stats.dist_desde_white} giros já passaram desde o white; "
                f"próximo será o {proximo_giro}º giro."
            )

        # 5º giro após o white -> entrada
        if stats.dist_desde_white == 4:
            action = "entrar_white"
            reason = "REGRA 5/8: próximo giro é o 5º após o último white (1ª tentativa)."

        # 8º giro após o white -> segunda entrada
        elif stats.dist_desde_white == 7:
            action = "entrar_white"
            reason = "REGRA 5/8: próximo giro é o 8º após o último white (2ª tentativa)."

    # guarda se ESTE giro gerou entrada para avaliar o próximo resultado
    LAST_DECISION_WAS_ENTRY = (action == "entrar_white")
    CURRENT_STATS = stats

    return DecisionResponse(
        action=action,
        reason=reason,
        stats=stats,
    )


@app.get("/stats", response_model=Stats)
def get_stats():
    """Retorna o estado atual do robô (para debug / painel)."""
    return CURRENT_STATS


@app.post("/reset", response_model=Stats)
def reset_stats():
    """Zera estatísticas e reseta contadores (uso manual)."""
    global CURRENT_STATS, LAST_DECISION_WAS_ENTRY
    CURRENT_STATS = Stats()
    LAST_DECISION_WAS_ENTRY = False
    return CURRENT_STATS
