# One-time Zoom setup — administrator only

Faculty sign in only to DocTutorials. They do not enter Zoom credentials, approve Zoom OAuth access, or configure redirect URLs.

## 1. Use one administrator-managed Zoom account

Create both developer apps below under the same Zoom account that owns your webinars and host users. The assigned Zoom host must be an active user of that account and have the required webinar license or alternative-host permission. The backend verifies the exact host identity and account before issuing any host credentials.

## 2. Configure the Meeting SDK app

In Zoom Marketplace, create or use a General App with Meeting SDK enabled. Save its Client ID and Client Secret on the server:

```text
ZOOM_MEETING_SDK_CLIENT_ID=YOUR_CLIENT_ID
ZOOM_MEETING_SDK_CLIENT_SECRET=YOUR_CLIENT_SECRET
```

Configure the public application domain for SDK embedding as required by your Zoom app. The Meeting SDK app and the Server-to-Server OAuth app must belong to the same Zoom account.

## 3. Configure Server-to-Server OAuth

Create or use a Server-to-Server OAuth app on that same account. Save its credentials only in `.env`:

```text
ZOOM_ACCOUNT_ID=YOUR_ACCOUNT_ID
ZOOM_S2S_CLIENT_ID=YOUR_CLIENT_ID
ZOOM_S2S_CLIENT_SECRET=YOUR_CLIENT_SECRET
```

Grant the scopes for these APIs, then activate or reactivate the app:

- `user:read:token:admin` — get the assigned host's ZAK using `GET /users/{userId}/token?type=zak`.
- `user:read:user:admin` — verify the configured host using `GET /users/{userId}`.
- Webinar read admin permission, such as `webinar:read:webinar:admin` — read the assigned webinar using `GET /webinars/{webinarId}`. Select the scope mapped to Get a webinar in your Marketplace app.

The backend obtains an account access token at `POST https://zoom.us/oauth/token` with `grant_type=account_credentials`. It verifies the assigned webinar and host, obtains a fresh host ZAK, and signs a role=1 SDK JWT. Faculty never authorize an OAuth app themselves.

An OAuth redirect URL is not required for this server-managed flow. Existing callback settings may remain; the legacy app routes return to DocTutorials without prompting Zoom login.

## 4. Assign faculty and webinars

1. Restart the app with `./scripts/run.sh` after changing credentials or configuration.
2. Sign in as DocTutorials admin using your application address. Local Python listens on port 8792; Docker uses port 8791.
3. Create a faculty app login using the assigned Zoom host email. An opaque Zoom user ID can optionally be entered under Advanced host identity.
4. Assign the webinar ID, passcode and course details.
5. Faculty sign in only to DocTutorials. Their single assigned webinar opens automatically; multiple assignments require a choice unless exactly one class is currently due.

## Troubleshooting

- Missing Meeting SDK credentials: configure the General App Client ID and Secret.
- Server OAuth authorization failed: check the S2S Account ID, Client ID, Client Secret and app activation.
- Unable to authorize assigned host: enable `user:read:token:admin` on the S2S app and confirm that the host is managed by your Zoom account.
- Cannot verify webinar host: check webinar/user read scopes, the webinar ID and the faculty-to-host mapping.
- Host identity or account mismatch: correct the assignment; access is intentionally denied until it matches.
- Browser camera, microphone or sharing blocked: allow the relevant browser and macOS privacy permissions.

Zoom's own webinar controls provide camera, microphone, screen sharing and cloud recording, subject to the account's permissions. Custom live MP4 composition is still outside this build.

Official references:

- [Start a meeting with a server-issued host token](https://developers.zoom.us/docs/isv/workflows/start-meeting/)
- [Users API — Get a user's token](https://developers.zoom.us/docs/api/users/)
- [Internal Meeting SDK authorization](https://developers.zoom.us/docs/meeting-sdk/auth/jwt/)
- [Meeting SDK webinar host parameters](https://developers.zoom.us/docs/meeting-sdk/web/client-view/meetings-webinars/)
