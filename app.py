from fastapi import FastAPI
from pydantic import BaseModel
from typing import Literal, Optional

# ---------- Tipos básicos ----------

SpinColor = Literal["red", "black", "white"]

app = FastAPI(title="Spectra X - White 5/8 SIMPLES")


class Stats(BaseModel):
    whites_today: int = 0
    losses_today: int = 0
    attempts_today: int = 0
    dist_desde_white: Optional[int] = None  # giros desde o último white (0 = acabou de sair)
    total_spins: int = 0                     # quantos resultados já recebemos


class DecisionResponse(BaseModel):
    action: Literal["aguardar", "entrar_white"]
    reason: str
    stats: Stats


class PushRoundPayload(BaseModel):
    number: int  # 0–14 vindo da Blaze


CURRENT_STATS = Stats()
LAST_DECISION_WAS_ENTRY: bool = False  # se o giro ANTERIOR era sinal de entrada


def number_to_color(num: int) -> SpinColor:
    if num == 0:
        return "white"
    if num <= 7:
        return "red"
    return "black"


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
    stats.total_spins += 1

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
        # acabou de sair white -> esse giro é o 0 depois do white
        stats.dist_desde_white = 0
    else:
        if stats.dist_desde_white is not None:
            # já tínhamos visto um white, então somamos +1
            stats.dist_desde_white += 1
        # se nunca vimos white (None), continua None

    # 3) Decide se o PRÓXIMO giro é 5º ou 8º após white
    action: Literal["aguardar", "entrar_white"] = "aguardar"
    reason = "Ainda não saiu white; aguardando primeiro white."

    if stats.dist_desde_white is not None:
        # dist_desde_white = quantos giros JÁ passaram depois do white (incluindo o atual).
        # Se dist = 0 -> acabou de sair white
        # Se dist = 1 -> 1º giro após o white
        # ...
        proximo = stats.dist_desde_white + 1  # o PRÓXIMO giro após este resultado

        if stats.dist_desde_white == 0:
            reason = "White acabou de sair; próximo será o 1º giro após o white."
        else:
            reason = (
                f"{stats.dist_desde_white} giros já passaram desde o white; "
                f"próximo será o {proximo}º giro."
            )

        if stats.dist_desde_white == 4:  # já passaram 4 -> próximo é o 5º
            action = "entrar_white"
            reason = "REGRA 5/8: próximo giro é o 5º após o último white (1ª tentativa)."
        elif stats.dist_desde_white == 7:  # já passaram 7 -> próximo é o 8º
            action = "entrar_white"
            reason = "REGRA 5/8: próximo giro é o 8º após o último white (2ª tentativa)."

    LAST_DECISION_WAS_ENTRY = (action == "entrar_white")
    CURRENT_STATS = stats

    return DecisionResponse(
        action=action,
        reason=reason,
        stats=stats,
    )


@app.get("/stats", response_model=Stats)
def get_stats():
    """Ver estado atual (para debug)."""
    return CURRENT_STATS


@app.post("/reset", response_model=Stats)
def reset_stats():
    """Zera estatísticas e histórico."""
    global CURRENT_STATS, LAST_DECISION_WAS_ENTRY
    CURRENT_STATS = Stats()
    LAST_DECISION_WAS_ENTRY = False
    return CURRENT_STATS
