# -*- coding: utf-8 -*-
class AgentRegistry:
    def __init__(self, bus, state):
        self.bus = bus
        self.state = state
        self.agents = {}

    def register(self, agent):
        name = agent.name
        self.agents[name] = agent
        self.state.update("agents", {name: {"status": "registered"}})

    def status(self):
        return {name: a.status() for name, a in self.agents.items()}
