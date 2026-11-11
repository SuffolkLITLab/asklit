# Static Next.js integration

`AskLitChat.tsx` is a client component. It uses only browser fetch calls against
the hosted AskLit public API and is compatible with `output: 'export'`.

1. Deploy the AskLit backend using [the deployment guide](../../docs/hosted-deployment.md).
2. Add the frontend's exact HTTPS origin to backend `ASKLIT_CORS_ORIGINS`.
3. Set `NEXT_PUBLIC_ASKLIT_API_URL=https://YOUR-ASKLIT-HOST` when building Next.js.
   This is a public API origin, not a secret. Never put model keys or SSO secrets
   in a `NEXT_PUBLIC_` variable.
4. Copy the component into the frontend and pass a published bot ID:

```tsx
<AskLitChat botId="THE_32_CHARACTER_BOT_ID" />
```

CourtFormsOnline has the component installed at
`src/app/components/AskLitChat.tsx`. Its `/asklit` page reads a bot ID in the
browser, so one static page serves newly published bots without a rebuild:

```text
https://courtformsonline.org/asklit?bot=THE_32_CHARACTER_BOT_ID
```

Optional form metadata `asklit_id` embeds that assistant on the form's existing
landing page. The form page adds the panel only when the API origin and a valid
bot ID are configured. This metadata is consumed during the site's ordinary
static build. Changes to a bot's published content do not need a frontend rebuild.

The component uses the site's Bootstrap styles and renders answers/source
passages as text. It keeps conversation capabilities only in memory, sends them
in an Authorization header, and omits cookies. Navigating away loses local
conversation access; server retention cleanup still applies. No API token is
put in a URL, local storage, or generated HTML.

The API chain is:

```text
GET    /api/public/bots/{bot_id}                 display metadata only
POST   /api/public/bots/{bot_id}/conversations   select a published prompt
POST   /api/public/conversations/{id}/messages  {message, request_id}
DELETE /api/public/conversations/{id}           delete the conversation
```

Only the last two operations need the conversation bearer token. All generation
uses the server's stored configuration and source revision. Public source
passages are deliberately visible in responses; publish only sources intended
for the bot's audience. CORS restricts cooperating browsers, not direct HTTP
clients; server-side spending and session limits also apply.

`AskLitChat.tsx` is kept as a reusable copy here and installed in the sibling
CourtFormsOnline repository. Apply component fixes to both copies. The static
frontend does not use Next.js API routes, server actions, or runtime cookies.
See [Next.js static export documentation](https://nextjs.org/docs/app/guides/static-exports).
