"""Health checks e validações de inicialização do servidor MCP.

Dois mecanismos distintos, com semânticas propositalmente separadas:

- `preflight`: validações determinísticas de configuração, executadas antes
  da aplicação subir. Falha ⇒ o processo não inicia.
- `registry` / `checks`: sondagens de dependências em runtime, expostas em
  `/health/detail`. Falha ⇒ o serviço é reportado como degradado, mas
  continua servindo.
"""
