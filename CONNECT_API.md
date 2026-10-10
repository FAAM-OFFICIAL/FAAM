# FAAM Connect — sign FAAM's apps in to a FAAM account

FAAM Connect lets FAAM's own apps (the **FAAM iOS App** first) connect to someone's
FAAM account and move their data over: watchlist, portfolio, practice trading and
FAAM Learn progress. The app never sees the user's password.

It is the standard native-app OAuth 2 flow (authorization code + PKCE), so
`ASWebAuthenticationSession` or the AppAuth library work as-is.

## Apps

Only the **dev** account can register apps: FAAM → Account → *Developer · Apps
that can connect*. The FAAM iOS App comes pre-registered:

| Client ID  | Name          | Return link        |
|------------|---------------|--------------------|
| `faam-ios` | FAAM iOS App  | `faam://connected` |

The return link must match exactly. Register the `faam` URL scheme in the iOS
app's Info.plist (URL Types).

## 1. Send the user to FAAM

Make a random `code_verifier` (43–128 chars) and its
`code_challenge = BASE64URL(SHA256(code_verifier))` without `=` padding. Then open:

```
https://<your FAAM server>/connect
    ?client_id=faam-ios
    &redirect_uri=faam://connected
    &response_type=code
    &code_challenge=<challenge>
    &code_challenge_method=S256
    &state=<random>
```

The user signs in (if needed) and sees **"Connect to FAAM iOS App?"** with
*Connect* / *Cancel*.

- Connect → `faam://connected?code=…&state=…`
- Cancel  → `faam://connected?error=access_denied&state=…`

Check that `state` matches what you sent.

## 2. Swap the code for a token

```
POST /api/connect/token
Content-Type: application/x-www-form-urlencoded   (JSON also accepted)

grant_type=authorization_code&code=…&client_id=faam-ios
&redirect_uri=faam://connected&code_verifier=…
```

```json
{ "access_token": "faam_…", "token_type": "Bearer", "expires_in": 7776000,
  "username": "alice", "app": "FAAM iOS App", "access": ["…"] }
```

Codes work once and expire after 10 minutes. Tokens last 90 days. Keep the token in
the Keychain. When any call returns **401**, run step 1 again.

## 3. Call the API

Send `Authorization: Bearer <access_token>` on every request. The browser session
cookie is not accepted here.

| Method | Path                | What it does |
|--------|---------------------|--------------|
| GET    | `/api/v1/me`        | `{username, plan, email}` |
| GET    | `/api/v1/export`    | Everything at once — use this to move data over: `{watchlist, portfolio, practice, learn, exportedAt}` |
| GET    | `/api/v1/watchlist` | `{symbols: ["NVDA", …]}` |
| POST   | `/api/v1/watchlist` | Replace it: `{symbols: [...]}` (up to 100 tickers) |
| GET    | `/api/v1/portfolio` | `{positions: [{id, symbol, shares, cost}]}` |
| POST   | `/api/v1/portfolio` | Replace it: `{positions: [{symbol, shares, cost, id?}]}` (up to 500) |
| GET    | `/api/v1/practice`  | Practice trading account: cash, equity, positions, recent trades |
| GET    | `/api/v1/learn`     | `{xp, streakDays, bestStreakDays, certificates, streakFreezes}` |

Errors look like `{"error": "…"}` with a 4xx status.

## Swift sketch

```swift
import AuthenticationServices
import CryptoKit

func connectToFAAM(server: URL, anchor: ASPresentationAnchor) async throws -> String {
    let verifier = Data((0..<48).map { _ in UInt8.random(in: 0...255) }).base64URL
    let challenge = Data(SHA256.hash(data: Data(verifier.utf8))).base64URL
    let state = UUID().uuidString

    var c = URLComponents(url: server.appendingPathComponent("connect"), resolvingAgainstBaseURL: false)!
    c.queryItems = [
        .init(name: "client_id", value: "faam-ios"),
        .init(name: "redirect_uri", value: "faam://connected"),
        .init(name: "response_type", value: "code"),
        .init(name: "code_challenge", value: challenge),
        .init(name: "code_challenge_method", value: "S256"),
        .init(name: "state", value: state),
    ]
    let callback: URL = try await withCheckedThrowingContinuation { cont in
        let s = ASWebAuthenticationSession(url: c.url!, callbackURLScheme: "faam") { url, err in
            if let url { cont.resume(returning: url) } else { cont.resume(throwing: err!) }
        }
        s.presentationContextProvider = /* your provider for `anchor` */ nil
        s.start()
    }
    let q = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems ?? []
    guard q.first(where: { $0.name == "state" })?.value == state,
          let code = q.first(where: { $0.name == "code" })?.value else { throw URLError(.userCancelledAuthentication) }

    var req = URLRequest(url: server.appendingPathComponent("api/connect/token"))
    req.httpMethod = "POST"
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    req.httpBody = try JSONSerialization.data(withJSONObject: [
        "grant_type": "authorization_code", "code": code, "client_id": "faam-ios",
        "redirect_uri": "faam://connected", "code_verifier": verifier,
    ])
    let (data, _) = try await URLSession.shared.data(for: req)
    let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
    guard let token = json?["access_token"] as? String else { throw URLError(.userAuthenticationRequired) }
    return token   // store in the Keychain, then GET /api/v1/export
}

extension Data {
    var base64URL: String {
        base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
}
```

## Where the app connects

The iOS app has to reach a FAAM server. The Mac app only listens on this Mac
(`localhost`), so a phone can't reach it. Point the app at a hosted FAAM backend
(see DEPLOY.md), with `FAAM_BASE_URL` set to its https address.
