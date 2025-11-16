from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Literal, Optional

# ---------- Tipos básicos ----------

SpinColor = Literal["red", "black", "white"]

app = FastAPI(title="Spectra X - White Hunter 5/8 (DEBUG 5/8 PURO)")


class Stats(BaseModel):
    whites_today: int = 0          # quantos brancos pegou hoje
    losses_today: int = 0          # quantas tentativas perdidas
    attempts_today: int = 0        # quantas entradas no white
    ciclos_sem_acerto: int = 0     # quantos ciclos ruins seguidos
    cooldown_giros_restantes: int = 0  # pausas de proteção (ainda mantemos, mas bem leve)


class Config(BaseModel):
    # Deixamos os stops BEM largos pra não travar seu teste
    stop_win_whites: int = 9999
    stop_loss_tentativas: int = 9999
    max_tentativas_por_ciclo: int = 2   # 5º e 8º giro
    min_score_para_operar: int = 0      # 0 = NÃO BLOQUEIA POR SCORE
    cooldown_ciclos_ruins: int = 9999   # na prática não entra em cooldown por ciclo
    cooldown_giros: int = 0             # sem pausa por giros
    janela_mercado: int = 30            # só pra cálculo de score/informação


class DecisionResponse(BaseModel):
    action: Literal["aguardar", "entrar_white", "parar"]
    reason: str
    score: int
    stats: Stats


class PushRoundPayload(BaseModel):
    number: int  # número de 0 a 14 vindo da Blaze


# ---------- Estado em memória ----------

CURRENT_STATS = Stats()
CONFIG = Config()
HISTORY: List[SpinColor] = []          # histórico de cores (mais antigo -> mais recente)
LAST_DECISION_WAS_ENTRY: bool = False  # se o último sinal foi "entrar_white"


# ---------- Funções de utilidade ----------

def number_to_color(num: int) -> SpinColor:
    """Converte número (0–14) em cor."""
    if num == 0:
        return "white"
    # padrão: 1–7 red, 8–14 black
    if num <= 7:
        return "red"
    return "black"


def giros_desde_ultimo_white(history: List[SpinColor]) -> Optional[int]:
    """Conta quantos giros passaram desde o último white."""
    if not history:
        return None
    count = 0
    for color in reversed(history):
        if color == "white":
            return count
        count += 1
    return None  # nunca saiu white


def calcular_score(history: List[SpinColor], stats: Stats, cfg: Config) -> int:
    """Score só informativo agora (não bloqueia mais entrada)."""
    if not history:
        return 50

    janela = history[-cfg.janela_mercado:] if len(history) > cfg.janela_mercado else history
    score = 50

    # 1) Distância desde o último white
    dist = giros_desde_ultimo_white(history)
    if dist is None:
        score -= 5
    else:
        if dist < 3:
            score -= 10
        elif 3 <= dist <= 8:
            score += 5
        elif 9 <= dist <= 20:
            score += 10
        else:
            score -= 5

    # 2) Sequências longas (red/black)
    same_streak = 1
    max_streak = 1
    last = janela[0]
    for c in janela[1:]:
        if c == last and c != "white":
            same_streak += 1
            max_streak = max(max_streak, same_streak)
        else:
            same_streak = 1
            last = c

    if max_streak >= 10:
        score -= 10
    elif max_streak >= 7:
        score -= 5
    elif max_streak <= 3:
        score += 5

    # clamp
    return max(0, min(100, score))


def checar_stops(stats: Stats, cfg: Config):
    """Mantemos só pra não quebrar, mas na prática nunca vai parar."""
    if stats.whites_today >= cfg.stop_win_whites:
        return "parar", f"Stop win de whites atingido ({stats.whites_today})."
    if stats.losses_today >= cfg.stop_loss_tentativas:
        return "parar", f"Stop loss de tentativas atingido ({stats.losses_today})."
    if stats.cooldown_giros_restantes > 0:
        return "aguardar", f"Em cooldown de proteção ({stats.cooldown_giros_restantes} giros restantes)."
    return None, None


def analisar_regra_5e8(history: List[SpinColor]) -> tuple[str, str]:
    """
    Implementa a regra 5/8:
    - ENTRAR no 5º giro após o white (primeira tentativa)
    - ENTRAR no 8º giro após o white (segunda tentativa)
    """
    if not history:
        return "aguardar", "Sem histórico suficiente."

    idx_last_white = None
    for i in range(len(history) - 1, -1, -1):
        if history[i] == "white":
            idx_last_white = i
            break

    if idx_last_white is None:
        return "aguardar", "Ainda não saiu white."

    giros_desde_white = (len(history) - 1) - idx_last_white
    proximo_numero = giros_desde_white + 1  # próximo giro após o white

    if proximo_numero == 5:
        return "entrar_white", "Regra 5/8: PRÓXIMO giro é o 5º após o último white (primeira tentativa)."
    if proximo_numero == 8:
        return "entrar_white", "Regra 5/8: PRÓXIMO giro é o 8º após o último white (segunda tentativa)."

    return "aguardar", f"{proximo_numero}º giro após o white, aguardando 5º ou 8º."


# ---------- Endpoint principal usado pelo SubBot ----------

@app.post("/api/push_round", response_model=DecisionResponse)
def push_round(payload: PushRoundPayload):
    """
    Chamado pelo SubBot a cada novo número que aparecer no histórico da Blaze.
    1) Atualiza histórico e stats do round ANTERIOR (se tinha sinal).
    2) Calcula decisão para o PRÓXIMO round (entrar no white ou não) pela regra 5/8.
    """
    global CURRENT_STATS, CONFIG, HISTORY, LAST_DECISION_WAS_ENTRY

    cfg = CONFIG
    stats = CURRENT_STATS

    # 1) Converte número em cor
    color = number_to_color(payload.number)

    # 2) Atualiza stats do sinal anterior (se o último round tinha pedido entrada)
    if LAST_DECISION_WAS_ENTRY:
        stats.attempts_today += 1
        if color == "white":
            stats.whites_today += 1
            stats.ciclos_sem_acerto = 0
        else:
            stats.losses_today += 1
            stats.ciclos_sem_acerto += 1

    # 3) Atualiza histórico com o resultado atual
    HISTORY.append(color)

    # 4) Decrementa cooldown, se algum dia usar
    if stats.cooldown_giros_restantes > 0:
        stats.cooldown_giros_restantes -= 1

    # 5) Stops (na prática, bem largos agora)
    acao_stop, motivo_stop = checar_stops(stats, cfg)
    score = calcular_score(HISTORY, stats, cfg)

    if acao_stop is not None:
        LAST_DECISION_WAS_ENTRY = False
        CURRENT_STATS = stats
        return DecisionResponse(
            action=acao_stop,
            reason=motivo_stop,
            score=score,
            stats=stats,
        )

    # 6) Regra 5/8 -> decide se o PRÓXIMO giro merece entrada
    acao_regra, motivo_regra = analisar_regra_5e8(HISTORY)

    LAST_DECISION_WAS_ENTRY = (acao_regra == "entrar_white")
    CURRENT_STATS = stats

    return DecisionResponse(
        action=acao_regra,
        reason=motivo_regra,
        score=score,
        stats=stats,
    )


# ---------- Endpoints auxiliares ----------

@app.get("/stats", response_model=Stats)
def get_stats():
    """Ver estado atual (para painel / debug)."""
    return CURRENT_STATS


@app.post("/reset", response_model=Stats)
def reset_stats():
    """Zera estatísticas e histórico (usar 1x por dia, no início)."""
    global CURRENT_STATS, HISTORY, LAST_DECISION_WAS_ENTRY
    CURRENT_STATS = Stats()
    HISTORY = []
    LAST_DECISION_WAS_ENTRY = False
    return CURRENT_STATS
