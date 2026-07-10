# Resumo de Rota (Sitrax) — nuvem + celular

App web pensado para **celular** e **servidor na nuvem** (Railway, etc.).

## O que o usuário vê no celular

1. Escolhe **1 placa** ou **todos**
2. Escolhe a data
3. Toca em **Gerar resumo**
4. Lê o texto e, se quiser, baixa **1 PDF de resumo**

Exemplo do resumo:

```
🔑 Ligou às 06:01
• de 06:20 às 09:07 esteve em Abreu e Lima
• de 10:27 às 11:48 esteve em Recife
🔒 Desligou às 13:10 em Moreno
```

## O que NÃO vai para o celular

| Arquivo | Onde fica | Destino |
|---------|-----------|---------|
| PDF bruto do Sitrax (histórico completo) | Pasta **temporária no servidor** | **Apagado** após o resumo |
| Scrape / HTML de debug | Só no servidor | Não enviado |
| **PDF de resumo** | Memória do servidor → download | **Único** arquivo opcional no celular |

## Arquitetura

```
[Celular]  --escolhe placa-->  [Servidor na nuvem]
                                    |
                                    | login Sitrax (credenciais no .env do servidor)
                                    | download PDF bruto → /tmp/sitrax_job_xxx/
                                    | parse → monta resumo
                                    | apaga /tmp/sitrax_job_xxx/   ← bruto some
                                    v
[Celular]  <-- 1 PDF resumo --  [Servidor]
```

- Credenciais do rastreador ficam **só no servidor** (`.env` / variáveis do Railway).
- Nada de download do Sitrax na galeria ou “Arquivos” do telefone.

## Rodar local

```powershell
cd C:\Users\TRANSRAP05\sitrax-relatorio-bot
.\.venv\Scripts\Activate.ps1
# configure .env (SITRAX_CLIENTE, SITRAX_USUARIO, SITRAX_SENHA)
python run.py serve
```

Abra no celular (mesma rede) ou no PC: http://SEU-IP:8000

### Só o resumo a partir de um PDF que já existe no PC (teste)

```powershell
python run.py report-pdf "C:\Users\...\HistoricoPosicoes_....pdf" --data 10/07/2026 --out resumo.txt
```

Gera também PDF-resumo pelo pipeline:

```powershell
python -c "from app.bot.pipeline import report_from_sitrax_pdf; r=report_from_sitrax_pdf(r'CAMINHO.pdf', data_ref='10/07/2026'); open(r.pdf_filename,'wb').write(r.pdf_bytes); print(r.texto)"
```

## Deploy (Railway / Docker)

1. Credenciais como variáveis de ambiente (nunca no app do celular)
2. `Dockerfile` sobe Chrome + app
3. `SITRAX_HEADLESS=true`
4. Usuário acessa a URL pública no celular

## Pastas importantes

| Arquivo | Função |
|---------|--------|
| `app/main.py` | Site mobile + download só do resumo |
| `app/bot/pipeline.py` | Temp workspace + apaga brutos |
| `app/bot/summary_pdf.py` | PDF-resumo limpo |
| `app/bot/pdf_parser.py` | Lê PDF bruto do Sitrax (só no servidor) |
| `app/bot/sitrax.py` | Automação (download vai para pasta temp) |
| `app/bot/report.py` | Texto: cidade / horário |

## Segurança

- Não commitar `.env`
- Preferir conta Sitrax só de consulta
- Um job de Chrome por vez (`ThreadPoolExecutor(max_workers=1)`)
