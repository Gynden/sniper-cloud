# bankroll.py
class BankrollAdvisor:
    def __init__(self, banca=20.0, frac_base=0.01, frac_cap=0.02):
        self.banca=banca; self.frac_base=frac_base; self.frac_cap=frac_cap
    def set_banca(self, v): self.banca=float(v)

    def stake_sugerida(self, p_win_est, odds_net=1.0):
        # Kelly simplificado c/ cap; se p_win_est for None, usa base.
        if p_win_est is None: return round(self.banca*self.frac_base,2)
        edge = max(0.0, (p_win_est*odds_net - (1.0 - p_win_est)))
        f = min(self.frac_cap, max(self.frac_base, edge/(odds_net+1e-6)))
        return round(self.banca*f, 2)
