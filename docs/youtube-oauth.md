# OAuth do YouTube no AutoShorts

Esta etapa conecta um `PublishingChannel` a um canal real do YouTube. Ela não
faz upload, publicação ou agendamento.

## Preparação no Google Cloud

1. Crie ou selecione um projeto Google Cloud para o AutoShorts.
2. Ative a **YouTube Data API v3**.
3. Configure a tela de consentimento OAuth e os usuários de teste necessários.
4. Crie um OAuth Client do tipo **Desktop app**.
5. Baixe o `client_secret.json` e guarde-o fora do repositório.
6. Defina a variável sem copiar ou imprimir o conteúdo:

   ```bash
   export AUTOSHORTS_GOOGLE_CLIENT_SECRET=/caminho/fora/do/repo/client_secret.json
   ```

7. Instale o suporte opcional:

   ```bash
   pip install -e '.[youtube-oauth]'
   ```

8. Abra a GUI, selecione um canal e use **Conectar YouTube**.

O navegador abre o fluxo oficial para aplicativos instalados, usando redirect
loopback em `127.0.0.1`. O AutoShorts pede acesso offline e o único scope é
`https://www.googleapis.com/auth/youtube.upload`. A mesma credencial é usada em
`channels.list(part="snippet", mine=true)` para confirmar o canal autenticado.
Não foi adicionado `youtube.readonly` porque essa identificação funciona dentro
do escopo já necessário para o upload futuro.

## Confirmação explícita

Após o OAuth, a GUI mostra título e `youtube_channel_id`. Nada é persistido até
o usuário confirmar. O binding não é trocado automaticamente caso uma verificação
posterior retorne outro ID.

## Armazenamento

Tokens ficam exclusivamente em:

`~/.config/autoshorts/youtube-credentials/<publishing_channel_id>.json`

O diretório usa permissão `0700` e cada arquivo `0600`. O diretório pode ser
alterado por `AUTOSHORTS_CREDENTIALS_DIR`. SQLite guarda apenas metadados não
secretos e a referência ao arquivo. Tokens, client secret e cookies nunca vão
para o JSON de configuração ou SQLite.

Cada `PublishingChannel` possui arquivo e binding próprios. Dois canais podem
referenciar a mesma `GoogleIdentity`, mas continuam com autorizações isoladas;
isso evita presumir que um token possa selecionar silenciosamente canais
diferentes.

## Verificar, reconectar e desconectar

**Verificar conexão** carrega a credencial, renova o access token quando
necessário, chama `channels.list(mine=true)` e compara o ID retornado com o ID
persistido. Divergência invalida a operação e nunca altera o destino.

**Reconectar** repete OAuth e exige nova confirmação. **Desconectar** remove o
arquivo secreto e o binding SQLite, sem excluir o canal ou a Google identity.

## Limitações atuais

- Não existe upload ou scheduler.
- A seleção prática entre vários Brand Accounts/canais gerenciados depende da
  conta e da tela de autorização do Google. Cada resultado deve ser confirmado
  pelo ID exibido; o AutoShorts não assume comportamento 1:1 por email.
- O primeiro teste real requer interação humana no navegador para cada canal.

Referências oficiais:

- https://developers.google.com/youtube/data/guides/auth/installed-apps
- https://developers.google.com/youtube/data/guides/implementation/channels
