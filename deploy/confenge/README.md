# Quickly Confenge — implantação reproduzível

Este bundle usa a variante Confenge do Quickly **v2.4.0**, construída a partir do commit-fonte fixado neste fork. A imagem é publicada pelo workflow **Confenge GHCR image** como `ghcr.io/tjsasakifln/quickly-confenge`; o VPS deve receber exclusivamente a referência imutável por digest que o workflow produzir. Não altera o clone-fonte nem instala proxy concorrente.

## Decisões encerradas

Hostinger SMTP/IMAP permanece o transporte. Quickly é o painel de campanha; sem Make, n8n, IA, Warmbly, extra-cli, aquecimento ou validação paga como pré-requisito. Campanhas comerciais só são ativadas pelo operador. Fuso operacional: `America/Sao_Paulo` (a janela e volumes são configurados no painel).

## Instalação no VPS

1. Copie este diretório para `/opt/quickly-confenge`, execute `cp .env.example .env`, preencha apenas os marcadores e `chmod 600 .env scripts/*.sh`. Substitua `QUICKLY_IMAGE` pelo digest de `ghcr.io/tjsasakifln/quickly-confenge` gerado pelo workflow. O bootstrap cria `tiago.sasaki` / `tiago.sasaki@confenge.com.br`; preencha a senha apenas na cópia local `.env`. Gere senha PostgreSQL e senha temporária do administrador com caracteres URL-safe; gere a chave Fernet com `python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())` em uma máquina segura.
2. Execute `docker compose -f compose.yml config`, depois `docker compose -f compose.yml up -d --wait`.
3. Execute `./scripts/bootstrap-admin.sh` **antes** de ativar o server block público; ele chama a API real `POST /api/auth/register`, que só aceita o primeiro usuário. Remova `QUICKLY_BOOTSTRAP_ADMIN_*` de `.env` após sucesso.
4. Adicione `nginx/campanhas.confenge.com.br.conf` ao nginx existente, adeque os caminhos de certificado à convenção já usada, valide com `nginx -t` e recarregue-o. O upstream é somente `127.0.0.1:19080`; PostgreSQL não tem porta publicada.

O compose cria o volume próprio `quickly_confenge_pgdata`, mantém os backups nativos no bind mount `/opt/quickly-confenge/app-backups`, e usa rede própria (a app conserva saída para SMTP/IMAP; Postgres continua sem porta publicada), reinício `unless-stopped`, healthchecks de Postgres e da rota pública de setup local, e logs Docker `local` com rotação. Uma única instância `app` executa o agendador. PostgreSQL 15 Alpine também está fixado por digest.

## Operação pelo painel

Em `https://campanhas.confenge.com.br`, entre com o administrador; em **Inboxes**, cadastre a conta SMTP/IMAP nativa (credenciais ficam criptografadas pela chave estável `QUICKLY_ENCRYPTION_KEY`). Para Hostinger use SMTP `smtp.hostinger.com` 465 TLS implícito (ou 587 STARTTLS após teste) e IMAP `imap.hostinger.com:993` TLS, com o e-mail completo.

Crie a campanha, importe `demo/contatos-demo.csv`, configure três etapas pausadas usando `{{assunto_1}}` / `{{mensagem_1}}`, depois `{{assunto_2}}` / `{{mensagem_2}}`, e `{{assunto_3}}` / `{{mensagem_3}}`. No v2.4.0, colunas além de `email` e `name` tornam-se campos personalizados e os modelos usam `{{campo}}`; o CSV usa domínios `.invalid` e não pode entregar. Confira a prévia por destinatário, janela, dias, limite e intervalo no painel antes de ativar uma campanha real.

## Manutenção e recuperação

`./scripts/backup.sh` cria `backups/quickly-confenge-*.tgz` (dump PostgreSQL, `.env` com as chaves estáveis e imagem), permissão 0600 e retenção de 30 dias. Proteja esse diretório como segredo. `./scripts/restore.sh ARQUIVO` para a app, recria o banco e restaura o dump somente quando a chave Fernet atual é igual à arquivada — impedindo recuperar dados sem conseguir descriptografar SMTP/IMAP. Para recuperação total em host novo, restaure com segurança `runtime.env` como `.env`, suba o compose e então execute o restore.

`./scripts/update.sh` faz backup, aceita somente a imagem autorizada e conserva snapshots `*.previous`; `./scripts/rollback.sh` retorna a configuração anterior, mas não tenta downgrade de banco — para incompatibilidade, restaure backup compatível. Esta versão fixa não prevê atualização para imagem diferente sem revisão explícita de compatibilidade.

## Verificação mínima

Após deploy: `curl -fsS http://127.0.0.1:19080/api/auth/setup-status`; `docker compose -f compose.yml ps`; valide `nginx -t` antes de reload. Os testes SMTP/IMAP, entrega a conta controlada e leitura sem marcar como lida devem ser feitos no painel com a credencial autorizada, nunca registrados neste bundle.
