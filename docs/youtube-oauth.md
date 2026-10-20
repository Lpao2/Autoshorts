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
loopback em `127.0.0.1`. O AutoShorts pede acesso offline e um único scope que
cobre o ciclo completo do canal:

- `https://www.googleapis.com/auth/youtube`, para identificar o canal, publicar
  vídeos e provisionar metadados/banner do canal. Esse escopo único evita nova
  autorização quando o canal avançar do onboarding para publicação, além de
  permitir a verificação por `channels.list(part="snippet", mine=true)`.

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

Uma `GoogleIdentity` representa a Conta Google organizacional e não contém
tokens. Cada `ChannelCredentialBinding` liga um `PublishingChannel` a exatamente
um YouTube Channel ID e referencia seu próprio arquivo secreto. Assim, uma mesma
Conta Google pode administrar vários canais, mas cada canal passa uma vez pelo
onboarding OAuth e recebe uma credencial persistente separada. Canais legados ou
arquivados podem permanecer desativados sem consumir um dos três slots ativos do
tema; isso preserva bindings e histórico durante trocas de canal.

## Verificar, reconectar e desconectar

**Verificar conexão** carrega a credencial, renova o access token quando
necessário, chama `channels.list(mine=true)` e compara o ID retornado com o ID
persistido. Divergência invalida a operação e nunca altera o destino.

**Reconectar** repete OAuth e exige nova confirmação. **Desconectar** remove o
arquivo secreto e o binding SQLite, sem excluir o canal ou a Google identity.

## Persistência e modo Testing

O fluxo solicita `access_type=offline` e `prompt=consent`. O refresh token fica
no arquivo `0600` do binding e a biblioteca Google renova o access token quando
necessário; o payload atualizado é regravado atomicamente após a validação.

O status Publishing do consent screen não pode ser inferido com segurança pelo
client secret ou pelo token local e deve ser conferido no Google Cloud. Segundo
a documentação oficial, apps External em `Testing` recebem refresh tokens com
validade de sete dias quando usam scopes além de identidade básica. Para um
scheduler persistente, o app deve chegar a `Production` e cumprir a verificação
aplicável antes da operação contínua.

## Publicação controlada

O binding confirmado pode ser usado pelo fluxo revisável descrito em
[`youtube-publishing-smoke-test.md`](youtube-publishing-smoke-test.md). Uma
publicação exige aprovação explícita, preflight sem upload e confirmação final;
o padrão é `private`. Credenciais revogadas ou inválidas passam a ser marcadas
no binding e precisam de reconexão.

## Limitações atuais

- Existe upload controlado, mas não scheduler automático.
- A seleção prática entre vários Brand Accounts/canais gerenciados depende da
  conta e da tela de autorização do Google. Cada resultado deve ser confirmado
  pelo ID exibido; o AutoShorts não assume comportamento 1:1 por email.
- O primeiro teste real requer interação humana no navegador para cada canal.

Referências oficiais:

- https://developers.google.com/youtube/data/guides/auth/installed-apps
- https://developers.google.com/youtube/data/guides/implementation/channels
- https://developers.google.com/identity/protocols/oauth2#expiration
