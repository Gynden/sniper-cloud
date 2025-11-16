from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Literal, Optional
from datetime import datetime

# Tipos
SpinColor = Literal["red", "black", "white"]

app = FastAPI(title="Spectra X - White Hunter 5/8")


class HistoryPayload(BaseModel):
    history: List[SpinColor]  # do mais antigo pro mais recente
    bankroll: Optional[float] = None
    now: Optional[datetime] = None


class Stats(BaseModel):
    whites_today: int = 0
    losses_today: int = 0
    attempts_today: int = 0
    ciclos_sem_acerto: int = 0
    cooldown_giros_restantes: int = 0


class Config(BaseModel):
    stop_win_whites: int = 5                # quantos brancos no dia para travar
    stop_loss_tentativas: int = 10          # quantas entradas perdidas para travar
    max_tentativas_por_ciclo: int = 2       # 5º e 8º giro
    hora_inicio: int = 0                    # horário de operação (0–23)
    hora_fim: int = 23
    min_score_para_operar: int = 40         # score mínimo do mercado
    cooldown_ciclos_ruins: int = 2          # quantos ciclos ruins ativam cooldown
    cooldown_giros: int = 40                # quantos giros dura o cooldown
    janela_mercado: int = 30                # quantos giros olhar para calcular score


class DecisionResponse(BaseModel):
    action: Literal["aguardar", "entrar_white", "parar"]
    reason: str
    score: int
    stats: Stats


class ResultPayload(BaseModel):
    entrou: bool
    foi_white: bool
    bankroll: Optional[float] = None


# Estado global simples (um perfil único)
CURRENT_STATS = Stats()
CONFIG = Config()


# ---------- Funções de utilidade ----------

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
    """Calcula um score 0–100 do 'estado do mercado'."""
    if not history:
        return 50

    janela = history[-cfg.janela_mercado:] if len(history) > cfg.janela_mercado else history
    score = 50

    # 1) Distância desde o último white
    dist = giros_desde_ultimo_white(history)
    if dist is None:
        score -= 10
    else:
        if dist < 3:
            score -= 15
        elif 3 <= dist <= 8:
            score += 5
        elif 9 <= dist <= 20:
            score += 10
        else:
            score -= 5

    # 2) Sequências muito longas de uma cor (só red/black)
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
        score -= 15
    elif max_streak >= 7:
        score -= 8
    elif max_streak <= 3:
        score += 5

    # 3) Desempenho do dia
    if stats.attempts_today >= 4:
        taxa = (stats.whites_today / stats.attempts_today) if stats.attempts_today > 0 else 0
        if taxa >= 0.15:
            score += 5
        elif taxa <= 0.05:
            score -= 10

    # 4) Muitas perdas acumuladas
    if stats.losses_today >= cfg.stop_loss_tentativas // 2:
        score -= 10

    return max(0, min(100, score))


def checar_stops(stats: Stats, cfg: Config):
    """Verifica stop win, stop loss e cooldown."""
    if stats.whites_today >= cfg.stop_win_whites:
        return "parar", f"Stop win de whites atingido ({stats.whites_today})."
    if stats.losses_today >= cfg.stop_loss_tentativas:
        return "parar", f"Stop loss de tentativas atingido ({stats.losses_today})."
    if stats.cooldown_giros_restantes > 0:
        return "aguardar", f"Em cooldown de proteção ({stats.cooldown_giros_restantes} giros restantes)."
    return None, None


def analisar_regra_5e8(history: List[SpinColor], stats: Stats, cfg: Config):
    """Implementa a regra: entrar no 5º e no 8º giro após o white."""
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
        return "entrar_white", "Regra 5/8: 5º giro após o último white (primeira tentativa)."
    if proximo_numero == 8:
        return "entrar_white", "Regra 5/8: 8º giro após o último white (segunda tentativa)."

    return "aguardar", f"{proximo_numero}º giro após o white, aguardando 5º ou 8º."


# ---------- Endpoints da API ----------

@app.post("/decidir", response_model=DecisionResponse)
def decidir(payload: HistoryPayload):
    """
    Chamar a cada novo giro com o histórico completo:
    - history: lista de cores (do mais antigo pro mais recente)
    - now: datetime opcional (pra respeitar horário de operação)
    """
    global CURRENT_STATS, CONFIG
    stats = CURRENT_STATS
    cfg = CONFIG

    # 1) Stops e cooldown
    acao_stop, motivo_stop = checar_stops(stats, cfg)
    if acao_stop is not None:
        score = calcular_score(payload.history, stats, cfg)
        return DecisionResponse(
            action=acao_stop,
            reason=motivo_stop,
            score=score,
            stats=stats
        )

    # 2) Horário (se informado)
    if payload.now is not None:
        hora = payload.now.hour
        if not (cfg.hora_inicio <= hora < cfg.hora_fim):
            score = calcular_score(payload.history, stats, cfg)
            return DecisionResponse(
                action="aguardar",
                reason=f"Fora do horário de operação ({cfg.hora_inicio}h–{cfg.hora_fim}h).",
                score=score,
                stats=stats
            )

    # 3) Score de mercado
    score = calcular_score(payload.history, stats, cfg)
    if score < cfg.min_score_para_operar:
        return DecisionResponse(
            action="aguardar",
            reason=f"Score de mercado baixo ({score}). Aguardando melhor momento.",
            score=score,
            stats=stats
        )

    # 4) Regra 5/8
    acao_regra, motivo_regra = analisar_regra_5e8(payload.history, stats, cfg)
    return DecisionResponse(
        action=acao_regra,
        reason=motivo_regra,
        score=score,
        stats=stats
    )


@app.post("/resultado", response_model=Stats)
def atualizar_resultado(result: ResultPayload):
    """
    Chamar depois que a rodada fechar:
    - entrou: True/False se o bot realmente entrou no white
    - foi_white: True/False se caiu white
    """
    global CURRENT_STATS, CONFIG
    stats = CURRENT_STATS
    cfg = CONFIG

    if result.entrou:
        stats.attempts_today += 1
        if result.foi_white:
            stats.whites_today += 1
            stats.ciclos_sem_acerto = 0
        else:
            stats.losses_today += 1
            stats.ciclos_sem_acerto += 1

    # Muitos ciclos ruins → ativa cooldown
    if stats.ciclos_sem_acerto >= cfg.cooldown_ciclos_ruins:
        stats.cooldown_giros_restantes = cfg.cooldown_giros
        stats.ciclos_sem_acerto = 0

    CURRENT_STATS = stats
    return stats


@app.post("/tick")
def tick():
    """
    Opcional: chamar a cada giro só para ir reduzindo o cooldown_giros_restantes.
    """
    global CURRENT_STATS
    if CURRENT_STATS.cooldown_giros_restantes > 0:
        CURRENT_STATS.cooldown_giros_restantes -= 1
    return {"cooldown_giros_restantes": CURRENT_STATS.cooldown_giros_restantes}


@app.get("/stats", response_model=Stats)
def get_stats():
    """Ver estado atual do dia."""
    return CURRENT_STATS


@app.post("/reset", response_model=Stats)
def reset_stats():
    """Zera estatísticas (usar 1x por dia, tipo no início do dia)."""
    global CURRENT_STATS
    CURRENT_STATS = Stats()
    return CURRENT_STATS
