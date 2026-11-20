import {useEffect, type ReactNode} from 'react';
import {CopyButton, ThemeToggle, Wordmark} from './ui';

// 文档里的地址一律取当前站点，部署到哪个域名都对
const ISSUER = location.origin;

const Code = ({children, label}: {children: string; label?: string}) => {
    const text = children.replace(/^\n/, '').replace(/\s+$/, '');
    return (
        <div className="code">
            <div className="code-head"><span>{label ?? ''}</span><CopyButton text={text} /></div>
            <pre><code>{text}</code></pre>
        </div>
    );
};

const Section = ({id, title, children}: {id: string; title: string; children: ReactNode}) => (
    <section id={id} className="doc-section">
        <h2><a href={`#${id}`}>{title}</a></h2>
        {children}
    </section>
);

const TOC: [string, string][] = [
    ['overview', '概览'],
    ['create', '创建应用'],
    ['flow', '登录流程'],
    ['authorize', '授权请求'],
    ['token', '换取令牌'],
    ['userinfo', '用户信息和 scope'],
    ['refresh', '刷新和吊销'],
    ['id-token', '校验 id_token'],
    ['logout', '登出'],
    ['native', '桌面和手机应用'],
    ['errors', '错误'],
    ['examples', '完整示例'],
    ['rules', '规则和限制']
];

const NODE_EXAMPLE = `
// Node.js 18+，npm i express express-session
import crypto from 'node:crypto';
import express from 'express';
import session from 'express-session';

const ISSUER = '${ISSUER}';
const CLIENT_ID = process.env.KUKEAUTH_CLIENT_ID;
const CLIENT_SECRET = process.env.KUKEAUTH_CLIENT_SECRET;
const REDIRECT_URI = 'https://example.com/auth/callback';

const random = bytes => crypto.randomBytes(bytes).toString('base64url');

const app = express();
app.use(session({secret: process.env.SESSION_SECRET, resave: false, saveUninitialized: false}));

app.get('/login', (req, res) => {
    const verifier = random(32);
    const state = random(16);
    req.session.oauth = {verifier, state};
    const url = new URL(\`\${ISSUER}/oauth/authorize\`);
    url.search = new URLSearchParams({
        response_type: 'code',
        client_id: CLIENT_ID,
        redirect_uri: REDIRECT_URI,
        scope: 'openid profile',
        state,
        code_challenge: crypto.createHash('sha256').update(verifier).digest('base64url'),
        code_challenge_method: 'S256'
    });
    res.redirect(url.href);
});

app.get('/auth/callback', async (req, res) => {
    const saved = req.session.oauth;
    delete req.session.oauth;
    if (!saved || req.query.state !== saved.state) return res.status(400).send('state 不匹配');
    if (req.query.error) return res.status(400).send(\`登录没有完成：\${req.query.error}\`);

    const tokenResponse = await fetch(\`\${ISSUER}/oauth/token\`, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({
            grant_type: 'authorization_code',
            code: req.query.code,
            redirect_uri: REDIRECT_URI,
            code_verifier: saved.verifier,
            client_id: CLIENT_ID,
            client_secret: CLIENT_SECRET
        })
    });
    if (!tokenResponse.ok) return res.status(400).send(await tokenResponse.text());
    const tokens = await tokenResponse.json();

    const user = await (await fetch(\`\${ISSUER}/oauth/userinfo\`, {
        headers: {Authorization: \`Bearer \${tokens.access_token}\`}
    })).json();

    // 用 sub 作为用户的唯一标识，用户名可能会变
    req.session.user = {id: user.sub, name: user.name, avatar: user.picture};
    res.redirect('/');
});

app.listen(3000);
`;

const PYTHON_EXAMPLE = `
# Python 3.10+，pip install flask requests
import base64, hashlib, os, secrets
from urllib.parse import urlencode

import requests
from flask import Flask, abort, redirect, request, session

ISSUER = "${ISSUER}"
CLIENT_ID = os.environ["KUKEAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["KUKEAUTH_CLIENT_SECRET"]
REDIRECT_URI = "https://example.com/auth/callback"

app = Flask(__name__)
app.secret_key = os.environ["SESSION_SECRET"]


@app.get("/login")
def login():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    session["oauth"] = {"verifier": verifier, "state": state}
    return redirect(f"{ISSUER}/oauth/authorize?" + urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }))


@app.get("/auth/callback")
def callback():
    saved = session.pop("oauth", None)
    if not saved or request.args.get("state") != saved["state"]:
        abort(400, "state 不匹配")
    if "error" in request.args:
        abort(400, f"登录没有完成：{request.args['error']}")

    tokens = requests.post(f"{ISSUER}/oauth/token", data={
        "grant_type": "authorization_code",
        "code": request.args["code"],
        "redirect_uri": REDIRECT_URI,
        "code_verifier": saved["verifier"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=10)
    tokens.raise_for_status()

    user = requests.get(f"{ISSUER}/oauth/userinfo", timeout=10, headers={
        "Authorization": f"Bearer {tokens.json()['access_token']}",
    }).json()

    # 用 sub 作为用户的唯一标识，用户名可能会变
    session["user"] = {"id": user["sub"], "name": user.get("name"), "avatar": user.get("picture")}
    return redirect("/")
`;

export const Docs = () => {
    useEffect(() => {
        document.title = 'KukeAuth 开发者文档';
        // 带着锚点打开时，页面是 React 画出来的，浏览器自己跳不到
        if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
    }, []);

    return (
        <div className="docs">
            <header className="topbar docs-topbar">
                <div className="docs-brand"><Wordmark href="/docs" /><span>开发者文档</span></div>
                <div className="topbar-side">
                    <a className="button" href="/account?tab=developer">管理我的应用</a>
                    <ThemeToggle />
                </div>
            </header>
            <div className="docs-body">
                <nav className="docs-toc" aria-label="目录">
                    {TOC.map(([id, title]) => <a key={id} href={`#${id}`}>{title}</a>)}
                </nav>
                <article className="docs-main">
                    <h1>用 KukeAuth 账号登录你的应用</h1>
                    <p className="docs-lead">
                        KukeAuth 是 KukeChat 的统一账号。任何人都可以免费接入：创建一个应用，拿到 client_id，按标准的 OAuth 2.0 / OpenID Connect 流程对接即可，不需要人工审核。
                    </p>

                    <Section id="overview" title="概览">
                        <ul>
                            <li>协议：OAuth 2.0 授权码模式 + PKCE（S256），兼容 OpenID Connect。现成的 OIDC 库填上发现地址就能用。</li>
                            <li>账号：KukeChat 的账号可以直接登录；没有账号的用户在登录页注册。</li>
                            <li>用户必须验证过邮箱或绑定过 CCW 才能授权给应用。没验证的人会被自动带去验证，接入方不用处理。</li>
                        </ul>
                        <table>
                            <tbody>
                                <tr><th>Issuer</th><td><code>{ISSUER}</code></td></tr>
                                <tr><th>发现文档</th><td><code>{ISSUER}/.well-known/openid-configuration</code></td></tr>
                                <tr><th>授权</th><td><code>GET {ISSUER}/oauth/authorize</code></td></tr>
                                <tr><th>换取令牌</th><td><code>POST {ISSUER}/oauth/token</code></td></tr>
                                <tr><th>用户信息</th><td><code>GET {ISSUER}/oauth/userinfo</code></td></tr>
                                <tr><th>吊销</th><td><code>POST {ISSUER}/oauth/revoke</code></td></tr>
                                <tr><th>登出</th><td><code>GET {ISSUER}/oauth/logout</code></td></tr>
                                <tr><th>公钥（JWKS）</th><td><code>{ISSUER}/oauth/jwks</code></td></tr>
                            </tbody>
                        </table>
                    </Section>

                    <Section id="create" title="创建应用">
                        <ol>
                            <li>登录 KukeAuth，打开 <a className="link" href="/account?tab=developer">我的账号 → 开发者</a>。账号需要先验证邮箱或绑定 CCW。</li>
                            <li>点「新建应用」，填写名称、回调地址，勾选需要的 scope，选择应用类型。</li>
                            <li>创建后会显示 <code>client_id</code> 和 <code>client_secret</code>。secret 只显示这一次，请立即保存到服务端的配置里；丢了可以重置，旧的随即失效。</li>
                        </ol>
                        <table>
                            <thead><tr><th>应用类型</th><th>适用</th><th>换令牌时</th></tr></thead>
                            <tbody>
                                <tr><td>有后端的应用</td><td>网站、带服务端的程序</td><td>带 <code>client_secret</code> 和 PKCE</td></tr>
                                <tr><td>公开应用</td><td>桌面程序、手机 App</td><td>只带 <code>client_id</code> 和 PKCE</td></tr>
                            </tbody>
                        </table>
                        <p>纯网页前端（没有后端的单页应用）目前不支持：令牌接口不对任意网站开放跨域请求。请用一个后端来换令牌。</p>
                    </Section>

                    <Section id="flow" title="登录流程">
                        <ol>
                            <li>你的后端生成随机的 <code>state</code> 和 PKCE 的 <code>code_verifier</code>，存进用户的会话。</li>
                            <li>把用户跳转到 <code>/oauth/authorize</code>。用户在 KukeAuth 登录并确认授权。</li>
                            <li>KukeAuth 跳回你的回调地址，带上 <code>code</code>、<code>state</code> 和 <code>iss</code>。</li>
                            <li>你的后端核对 <code>state</code>，用 <code>code</code> 和 <code>code_verifier</code> 到 <code>/oauth/token</code> 换令牌。</li>
                            <li>用 access token 调 <code>/oauth/userinfo</code> 取用户信息，以 <code>sub</code> 识别用户，建立你自己的登录会话。</li>
                        </ol>
                        <p>用户之前已经授权过、且 KukeAuth 上仍处于登录状态时，第 2 步会直接跳回，不再显示确认页。</p>
                    </Section>

                    <Section id="authorize" title="授权请求">
                        <Code label="浏览器跳转">{`
${ISSUER}/oauth/authorize
    ?response_type=code
    &client_id=app-1234567890abcdef
    &redirect_uri=https%3A%2F%2Fexample.com%2Fauth%2Fcallback
    &scope=openid%20profile
    &state=随机字符串
    &code_challenge=BASE64URL(SHA256(code_verifier))
    &code_challenge_method=S256
`}</Code>
                        <table>
                            <thead><tr><th>参数</th><th>说明</th></tr></thead>
                            <tbody>
                                <tr><td><code>response_type</code></td><td>必填，固定为 <code>code</code></td></tr>
                                <tr><td><code>client_id</code></td><td>必填</td></tr>
                                <tr><td><code>redirect_uri</code></td><td>必填，必须和登记的某个回调地址完全相同</td></tr>
                                <tr><td><code>scope</code></td><td>必填，空格分隔，只能是应用登记过的 scope</td></tr>
                                <tr><td><code>state</code></td><td>必填，随机字符串，回调时原样带回，用来防 CSRF</td></tr>
                                <tr><td><code>code_challenge</code></td><td>必填，<code>code_verifier</code> 的 SHA-256 再做 base64url（不带 <code>=</code>），43 到 128 位</td></tr>
                                <tr><td><code>code_challenge_method</code></td><td>必填，固定为 <code>S256</code></td></tr>
                                <tr><td><code>nonce</code></td><td>选填，会原样写进 id_token</td></tr>
                                <tr><td><code>prompt</code></td><td>选填。<code>none</code>：不显示任何页面，不能静默完成就返回错误；<code>login</code>：要求用户重新输入密码；<code>consent</code>、<code>select_account</code>：总是显示确认页</td></tr>
                            </tbody>
                        </table>
                        <p><code>code_verifier</code> 是 43 到 128 位的随机字符串（字母、数字、<code>-._~</code>），每次登录新生成一个，只保存在你的服务端。</p>
                        <p>成功时跳回 <code>redirect_uri?code=…&amp;state=…&amp;iss={ISSUER}</code>。授权码 60 秒内有效，只能用一次。</p>
                    </Section>

                    <Section id="token" title="换取令牌">
                        <Code label="POST /oauth/token">{`
POST ${ISSUER}/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&code=回调里拿到的 code
&redirect_uri=https%3A%2F%2Fexample.com%2Fauth%2Fcallback
&code_verifier=第 1 步生成的 code_verifier
&client_id=app-1234567890abcdef
&client_secret=kcs_…
`}</Code>
                        <p>client 凭据也可以放在 <code>Authorization: Basic base64(client_id:client_secret)</code> 里。公开应用不带 secret。</p>
                        <Code label="响应">{`
{
  "access_token": "eyJhbGciOiJFUzI1NiIs…",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "openid profile",
  "id_token": "eyJhbGciOiJFUzI1NiIs…",
  "refresh_token": "…"
}
`}</Code>
                        <p><code>id_token</code> 只在申请了 <code>openid</code> 时返回，<code>refresh_token</code> 只在申请了 <code>offline_access</code> 时返回。令牌只应保存在你的服务端，不要交给浏览器。</p>
                    </Section>

                    <Section id="userinfo" title="用户信息和 scope">
                        <Code label="请求">{`
GET ${ISSUER}/oauth/userinfo
Authorization: Bearer <access_token>
`}</Code>
                        <Code label="响应（申请了全部 scope 时）">{`
{
  "sub": "acc_Zk3…",
  "preferred_username": "alice",
  "name": "爱丽丝",
  "picture": "https://…/avatar.png",
  "bio": "个人简介",
  "email": "alice@example.com",
  "email_verified": true,
  "ccw_verified": true,
  "ccw_student_oid": "…",
  "ccw_name": "…"
}
`}</Code>
                        <table>
                            <thead><tr><th>scope</th><th>返回的字段</th></tr></thead>
                            <tbody>
                                <tr><td><code>openid</code></td><td><code>sub</code>：用户的唯一标识，以 <code>acc_</code> 开头，永远不变。请用它作为你那边的用户主键</td></tr>
                                <tr><td><code>profile</code></td><td><code>preferred_username</code>（用户名，可能变更）、<code>name</code>（昵称）、<code>picture</code>（头像，可能为 null）、<code>bio</code></td></tr>
                                <tr><td><code>email</code></td><td><code>email</code>、<code>email_verified</code>。只有验证过的邮箱才会返回，没有时这两个字段不出现</td></tr>
                                <tr><td><code>ccw</code></td><td><code>ccw_verified</code>；绑定了 CCW 时还有 <code>ccw_student_oid</code>、<code>ccw_name</code></td></tr>
                                <tr><td><code>offline_access</code></td><td>不返回字段，用于获得 refresh token</td></tr>
                            </tbody>
                        </table>
                        <p>用户的资料可能随时修改，需要最新信息时重新调用 userinfo。用户被封禁、取消授权或应用被删除后，userinfo 返回 401。</p>
                    </Section>

                    <Section id="refresh" title="刷新和吊销">
                        <Code label="刷新">{`
POST ${ISSUER}/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&refresh_token=…&client_id=…&client_secret=…
`}</Code>
                        <ul>
                            <li>access token 有效期 1 小时，签发后无法提前作废。</li>
                            <li>refresh token 有效期 30 天。每次刷新都会返回新的 refresh token，旧的立即失效；请保存新的那个。</li>
                            <li>已失效的 refresh token 再次被使用时，KukeAuth 认为它泄露了，会吊销同一次登录得到的全部 refresh token。同一个 refresh token 不要并发刷新。</li>
                            <li>可以带 <code>scope</code> 参数申请更小的范围，不能比原来的大。</li>
                        </ul>
                        <Code label="吊销（用户在你的应用里退出时）">{`
POST ${ISSUER}/oauth/revoke
Content-Type: application/x-www-form-urlencoded

token=<refresh_token>&client_id=…&client_secret=…
`}</Code>
                        <p>无论令牌是否有效都返回 200。</p>
                    </Section>

                    <Section id="id-token" title="校验 id_token">
                        <p>只用 userinfo 取用户信息的话，可以不处理 id_token。如果要用它，请按下面的规则校验：</p>
                        <ul>
                            <li>签名算法 ES256（P-256），公钥在 <code>{ISSUER}/oauth/jwks</code>，按 JWT 头部的 <code>kid</code> 选择。请缓存公钥，遇到不认识的 <code>kid</code> 再重新获取。</li>
                            <li><code>iss</code> 等于 <code>{ISSUER}</code>，<code>aud</code> 等于你的 client_id，<code>exp</code> 未过期。</li>
                            <li>请求时带了 <code>nonce</code> 的话，核对 id_token 里的 <code>nonce</code>。</li>
                        </ul>
                        <p>其他字段：<code>sub</code>、<code>iat</code>、<code>auth_time</code>（用户在 KukeAuth 登录的时间）。access token 虽然也是 JWT，请把它当成不透明字符串，只用来调 userinfo。</p>
                    </Section>

                    <Section id="logout" title="登出">
                        <p>用户在你的应用里退出时，如果也要退出 KukeAuth，把浏览器跳转到：</p>
                        <Code>{`
${ISSUER}/oauth/logout?id_token_hint=<id_token>&post_logout_redirect_uri=https%3A%2F%2Fexample.com%2F&state=…
`}</Code>
                        <ul>
                            <li><code>id_token_hint</code> 必填，过期的也可以。</li>
                            <li><code>post_logout_redirect_uri</code> 选填，必须是应用里登记过的「登出后跳转地址」，不填则停在 KukeAuth 登录页。</li>
                        </ul>
                    </Section>

                    <Section id="native" title="桌面和手机应用">
                        <ul>
                            <li>创建应用时选择「公开应用」，没有 client secret，安全性完全依赖 PKCE。</li>
                            <li>桌面程序推荐在本机临时开一个端口接收回调：登记 <code>http://127.0.0.1/callback</code>，授权时可以用任意端口，比如 <code>http://127.0.0.1:53127/callback</code>。只认 <code>127.0.0.1</code> 和 <code>[::1]</code>，<code>localhost</code> 不行。</li>
                            <li>用系统浏览器打开授权页，不要用内嵌网页。</li>
                        </ul>
                    </Section>

                    <Section id="errors" title="错误">
                        <p><code>client_id</code> 或 <code>redirect_uri</code> 不对时，授权接口直接返回 400（<code>invalid_client</code> / <code>invalid_redirect_uri</code>），不会跳回你的网站。其他错误会跳回 <code>redirect_uri?error=…&amp;state=…</code>：</p>
                        <table>
                            <thead><tr><th>error</th><th>原因</th></tr></thead>
                            <tbody>
                                <tr><td><code>access_denied</code></td><td>用户点了取消</td></tr>
                                <tr><td><code>invalid_request</code></td><td>缺少 <code>state</code>、PKCE 参数不对或 <code>prompt</code> 取值不对</td></tr>
                                <tr><td><code>invalid_scope</code></td><td>申请了应用没有登记的 scope</td></tr>
                                <tr><td><code>unsupported_response_type</code></td><td><code>response_type</code> 不是 <code>code</code></td></tr>
                                <tr><td><code>login_required</code></td><td><code>prompt=none</code>，但用户没有登录 KukeAuth</td></tr>
                                <tr><td><code>consent_required</code></td><td><code>prompt=none</code>，但用户还没授权过这个应用</td></tr>
                                <tr><td><code>interaction_required</code></td><td><code>prompt=none</code>，但用户账号还没验证</td></tr>
                            </tbody>
                        </table>
                        <p>令牌接口出错时返回 JSON <code>{'{"error": "…", "error_description": "…"}'}</code>：</p>
                        <table>
                            <thead><tr><th>error</th><th>原因</th></tr></thead>
                            <tbody>
                                <tr><td><code>invalid_client</code>（401）</td><td>client_id 或 secret 不对，或应用已被删除、停用</td></tr>
                                <tr><td><code>invalid_grant</code></td><td>授权码过期、已用过、与请求不符，PKCE 校验失败，refresh token 无效，或用户已被封禁</td></tr>
                                <tr><td><code>invalid_scope</code></td><td>刷新时申请的 scope 超出原来的范围</td></tr>
                                <tr><td><code>unsupported_grant_type</code></td><td>只支持 <code>authorization_code</code> 和 <code>refresh_token</code></td></tr>
                            </tbody>
                        </table>
                        <p>请求过于频繁时返回 429，令牌接口每个 IP 每分钟 120 次。</p>
                    </Section>

                    <Section id="examples" title="完整示例">
                        <p>下面两个例子做的事情相同：<code>/login</code> 发起登录，<code>/auth/callback</code> 处理回调并取得用户。也可以直接用现成的 OIDC 库（比如 Node.js 的 openid-client、Python 的 Authlib），填入发现文档地址即可。</p>
                        <Code label="Node.js（Express）">{NODE_EXAMPLE}</Code>
                        <Code label="Python（Flask）">{PYTHON_EXAMPLE}</Code>
                    </Section>

                    <Section id="rules" title="规则和限制">
                        <ul>
                            <li>每个账号最多创建 10 个应用。</li>
                            <li>回调地址最多 10 个，必须是 https，或本机调试用的 <code>http://127.0.0.1</code>，不能带 <code>#</code>。</li>
                            <li>授权确认页会注明「第三方应用」和开发者的用户名。应用名称不能包含 KukeAuth、KukeChat，也不得冒充其他网站或组织。</li>
                            <li>只申请需要的 scope，并按用户授权的用途使用信息。用户可以随时在「我的账号 → 已授权应用」取消授权。</li>
                            <li>违反以上规则，或被用于盗号、欺诈的应用，管理员会直接停用。停用后用户无法再通过它登录。</li>
                        </ul>
                    </Section>
                </article>
            </div>
        </div>
    );
};
