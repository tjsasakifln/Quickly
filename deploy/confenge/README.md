# Quickly Confenge — implantação reproduzível

Este bundle usa a variante Confenge do Quickly **v2.4.0**, construída a partir do commit-fonte fixado neste fork. A imagem é publicada pelo workflow **Confenge GHCR image** como `ghcr.io/tjsasakifln/quickly-confenge`; o VPS deve receber exclusivamente a referência imutável por digest que o workflow produzir. Não altera o clone-fonte nem instala proxy concorrente.

## Decisões encerradas

Hostinger SMTP/IMAP permanece o transporte. Quickly é o painel de campanha; sem Make, n8n, IA, Warmbly, extra-cli, aquecimento ou validação paga como pré-requisito. Campanhas comerciais só são ativadas pelo operador. Fuso operacional: `America/Sao_Paulo` (a janela e volumes são configurados no painel).

## Instalação no VPS

1. Copie este diretório para `/opt/quickly-confenge`, execute `cp .env.example .env`, preencha apenas os marcadores, `chmod 600 .env` e `chmod 750 scripts/*.sh`. Substitua `QUICKLY_IMAGE` pelo digest de `ghcr.io/tjsasakifln/quickly-confenge` gerado pelo workflow. O bootstrap cria `tiago.sasaki` / `tiago.sasaki@confenge.com.br`; preencha a senha apenas na cópia local `.env`. Gere senha PostgreSQL e senha temporária do administrador com caracteres URL-safe; gere a chave Fernet com `python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())` em uma máquina segura.
2. Execute `docker compose -f compose.yml config --quiet`, depois `docker compose -f compose.yml up -d --wait`.
3. Execute `./scripts/bootstrap-admin.sh` **antes** de ativar o server block público; ele chama a API real `POST /api/auth/register`, que só aceita o primeiro usuário. Remova `QUICKLY_BOOTSTRAP_ADMIN_*` de `.env` após sucesso.
4. Faça TLS em duas fases: primeiro instale `nginx/campanhas.confenge.com.br.http-acme.conf`, crie o webroot `/var/www/certbot`, valide com `nginx -t` e recarregue. Emita o certificado pelo mecanismo ACME já adotado no host usando esse webroot. Somente depois de uma emissão bem-sucedida substitua o bloco pelo `nginx/campanhas.confenge.com.br.conf`, adeque os caminhos do certificado, valide `nginx -t` e recarregue. O upstream é somente `127.0.0.1:19080`; PostgreSQL não tem porta publicada.

O compose cria o volume próprio `quickly_confenge_pgdata`, mantém os backups nativos no bind mount `/opt/quickly-confenge/app-backups`, e usa rede própria (a app conserva saída para SMTP/IMAP; Postgres continua sem porta publicada), reinício `unless-stopped`, healthchecks de Postgres e da rota pública de setup local, e logs Docker `local` com rotação. Há limites efetivos do Compose para memória, CPU e PIDs; ambos os containers recebem `no-new-privileges`, e a aplicação opera sem capabilities Linux. Uma única instância `app` executa o agendador. PostgreSQL 15 Alpine também está fixado por digest.

Deixe o registro `campanhas` em modo **DNS-only**. O nginx sobrescreve todos os headers de encaminhamento usados pela aplicação a partir de `$remote_addr`, impedindo que um cliente os forje. Se um CDN/proxy confiável for habilitado, configure antes o módulo `real_ip` com a lista publicada e mantida de faixas confiáveis; não altere este comportamento apenas para obter o IP original.

## Operação pelo painel

Em `https://campanhas.confenge.com.br`, entre com o administrador; em **Inboxes**, cadastre a conta SMTP/IMAP nativa (credenciais ficam criptografadas pela chave estável `QUICKLY_ENCRYPTION_KEY`). Para Hostinger use SMTP `smtp.hostinger.com` 465 TLS implícito (ou 587 STARTTLS após teste) e IMAP `imap.hostinger.com:993` TLS, com o e-mail completo.

Crie a campanha, importe `demo/contatos-demo.csv`, configure três etapas pausadas usando `{{assunto_1}}` / `{{mensagem_1}}`, depois `{{assunto_2}}` / `{{mensagem_2}}`, e `{{assunto_3}}` / `{{mensagem_3}}`. No v2.4.0, colunas além de `email` e `name` tornam-se campos personalizados e os modelos usam `{{campo}}`; o CSV usa domínios `.invalid` e não pode entregar. Confira a prévia por destinatário, janela, dias, limite e intervalo no painel antes de ativar uma campanha real.

## Manutenção e recuperação

`./scripts/backup.sh` cria `backups/quickly-confenge-*.tgz` (dump PostgreSQL, digest da imagem e fingerprint não reversível da chave Fernet) e o respectivo `.sha256`, ambos com permissão 0600 e retenção de 30 dias. Ele deliberadamente **não** arquiva `.env`, senhas ou chaves; copie o par para armazenamento offsite criptografado — o disco local não é a única cópia. `./scripts/restore.sh ARQUIVO` exige o sidecar de checksum, a mesma chave Fernet configurada localmente e o mesmo digest de imagem. Se houver uma revisão explícita de compatibilidade, `./scripts/restore.sh --allow-image-mismatch ARQUIVO` permite somente a divergência de imagem; a divergência da chave continua bloqueada. Em recuperação total em host novo, recupere as variáveis estáveis por canal seguro, suba o compose com o digest compatível e então execute o restore.

Após revisão explícita de compatibilidade, `./scripts/update.sh ghcr.io/tjsasakifln/quickly-confenge@sha256:<digest>` baixa o alvo, faz backup da versão atualmente configurada, conserva snapshots `*.previous` e só então troca o digest. `./scripts/rollback.sh` retorna a configuração anterior, mas não tenta downgrade de banco — para incompatibilidade, restaure backup compatível.

## Verificação mínima

Após deploy: `curl -fsS http://127.0.0.1:19080/api/auth/setup-status`; `docker compose -f compose.yml ps`; valide `nginx -t` antes de reload. Os testes SMTP/IMAP, entrega a conta controlada e leitura sem marcar como lida devem ser feitos no painel com a credencial autorizada, nunca registrados neste bundle.
