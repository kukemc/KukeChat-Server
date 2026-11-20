import {useEffect, useRef, useState, type ChangeEvent} from 'react';
import {ApiError, api, type CcwChallenge, type Connection, type Grant, type Me, type OAuthRequestInfo} from './api';
import {DeveloperTab} from './developer';
import {Docs} from './docs';
import {AuthShell, Avatar, Field, Notice, ThemeToggle, Wordmark, useSubmit} from './ui';

const query = new URLSearchParams(location.search);
const requestId = query.get('request');
const withRequest = (path: string): string => (requestId ? `${path}?request=${encodeURIComponent(requestId)}` : path);
const go = (path: string): void => location.assign(path);
// 登录后回到原来的账号页标签；只认站内的 /account，免得被拿来做开放跳转
const next = query.get('next');
const afterLogin = (): void => go(requestId ? withRequest('/continue') : next && /^\/account(\?|$)/.test(next) ? next : '/account');

const SCOPE_LABELS: Record<string, string> = {
    profile: '用户名、昵称、头像和简介',
    email: '已验证的邮箱地址',
    ccw: '绑定的 CCW 账号',
    offline_access: '在你离开后保持登录'
};
const scopeList = (scopes: string[]): string[] => scopes.filter(scope => scope in SCOPE_LABELS).map(scope => SCOPE_LABELS[scope]);

// ---------------------------------------------------------------- 登录、注册、找回密码

const Login = () => {
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const {busy, error, form} = useSubmit();
    return (
        <AuthShell>
            <h1>登录</h1>
            <p className="lead">KukeChat 账号可以直接登录。</p>
            <form onSubmit={form(async () => {
                await api.login(username.trim(), password);
                afterLogin();
            })}>
                <Field label="用户名或邮箱">
                    <input autoFocus required autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} />
                </Field>
                <Field label="密码">
                    <input required type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} />
                </Field>
                <a className="link field-aside" href={withRequest('/forgot')}>忘记密码？</a>
                <Notice>{error}</Notice>
                <button className="button is-primary is-wide" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
            </form>
            <p className="switch">还没有账号？<a className="link" href={withRequest('/register')}>注册</a></p>
        </AuthShell>
    );
};

const Register = () => {
    const [username, setUsername] = useState('');
    const [nickname, setNickname] = useState('');
    const [password, setPassword] = useState('');
    const {busy, error, setError, form} = useSubmit();
    return (
        <AuthShell>
            <h1>创建账号</h1>
            <p className="lead">已有 KukeChat 账号就不用再注册了。</p>
            <form onSubmit={form(async () => {
                if (password.length < 8) {
                    setError('密码至少 8 位');
                    return;
                }
                await api.register(username.trim(), nickname.trim(), password);
                go(withRequest('/verify'));
            })}>
                <Field label="用户名" hint="3 到 50 位，可用字母、数字、下划线、点和连字符。">
                    <input autoFocus required minLength={3} maxLength={50} pattern="[A-Za-z0-9_.\-]+" autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} />
                </Field>
                <Field label="昵称">
                    <input maxLength={80} autoComplete="nickname" placeholder="选填，别人看到的名字" value={nickname} onChange={event => setNickname(event.target.value)} />
                </Field>
                <Field label="密码" hint="至少 8 位。">
                    <input required type="password" autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)} />
                </Field>
                <Notice>{error}</Notice>
                <button className="button is-primary is-wide" disabled={busy}>{busy ? '正在创建…' : '继续'}</button>
            </form>
            <p className="switch">已有账号？<a className="link" href={withRequest('/login')}>登录</a></p>
        </AuthShell>
    );
};

const ForgotEmail = () => {
    const [email, setEmail] = useState('');
    const [code, setCode] = useState('');
    const [password, setPassword] = useState('');
    const [sent, setSent] = useState(false);
    const {busy, error, form} = useSubmit();
    if (!sent) {
        return (
            <form onSubmit={form(async () => {
                await api.resetEmailSend(email.trim());
                setSent(true);
            })}>
                <Field label="已验证的邮箱">
                    <input autoFocus required type="email" autoComplete="email" value={email} onChange={event => setEmail(event.target.value)} />
                </Field>
                <Notice>{error}</Notice>
                <button className="button is-primary is-wide" disabled={busy}>{busy ? '正在发送…' : '发送验证码'}</button>
            </form>
        );
    }
    return (
        <form onSubmit={form(async () => {
            await api.resetEmailConfirm(email.trim(), code.trim(), password);
            go(withRequest('/login'));
        })}>
            <Notice kind="ok">如果这个邮箱验证过 KukeAuth 账号，验证码已经发过去了。</Notice>
            <Field label="验证码">
                <input autoFocus required inputMode="numeric" maxLength={6} autoComplete="one-time-code" value={code} onChange={event => setCode(event.target.value)} />
            </Field>
            <Field label="新密码" hint="至少 8 位。">
                <input required minLength={8} type="password" autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)} />
            </Field>
            <Notice>{error}</Notice>
            <button className="button is-primary is-wide" disabled={busy}>{busy ? '正在重置…' : '重置密码'}</button>
        </form>
    );
};

const ForgotCcw = () => {
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [challenge, setChallenge] = useState<{code: string; comment_url: string | null} | null>(null);
    const {busy, error, form} = useSubmit();
    if (!challenge) {
        return (
            <form onSubmit={form(async () => setChallenge(await api.resetCcwChallenge(username.trim())))}>
                <Field label="用户名">
                    <input autoFocus required autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} />
                </Field>
                <Notice>{error}</Notice>
                <button className="button is-primary is-wide" disabled={busy}>{busy ? '正在获取…' : '获取评论口令'}</button>
            </form>
        );
    }
    return (
        <form onSubmit={form(async () => {
            await api.resetCcwConfirm(username.trim(), challenge.code, password);
            go(withRequest('/login'));
        })}>
            <p className="step">用绑定的 CCW 账号，把这串口令发到指定作品的评论区：</p>
            <code className="passcode">{challenge.code}</code>
            {challenge.comment_url ? <a className="button is-wide" href={challenge.comment_url} target="_blank" rel="noreferrer">打开评论区</a> : null}
            <Field label="新密码" hint="至少 8 位。评论发出后再提交。">
                <input required minLength={8} type="password" autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)} />
            </Field>
            <Notice>{error}</Notice>
            <button className="button is-primary is-wide" disabled={busy}>{busy ? '正在核对…' : '重置密码'}</button>
        </form>
    );
};

const Forgot = () => {
    const [method, setMethod] = useState<'email' | 'ccw'>('email');
    return (
        <AuthShell>
            <h1>找回密码</h1>
            <p className="lead">用验证过的邮箱，或者绑定的 CCW 账号。</p>
            <div className="segmented" role="tablist">
                <button type="button" role="tab" aria-selected={method === 'email'} onClick={() => setMethod('email')}>邮箱</button>
                <button type="button" role="tab" aria-selected={method === 'ccw'} onClick={() => setMethod('ccw')}>CCW</button>
            </div>
            {method === 'email' ? <ForgotEmail /> : <ForgotCcw />}
            <p className="switch"><a className="link" href={withRequest('/login')}>返回登录</a></p>
        </AuthShell>
    );
};

// ---------------------------------------------------------------- 验证

const EmailVerifier = ({onDone}: {onDone: (me: Me) => void}) => {
    const [email, setEmail] = useState('');
    const [code, setCode] = useState('');
    const [sent, setSent] = useState(false);
    const {busy, error, run, form} = useSubmit();
    return (
        <form onSubmit={form(async () => onDone(await api.verifyEmail(email.trim(), code.trim())))}>
            <Field label="邮箱">
                <div className="input-row">
                    <input required type="email" autoComplete="email" value={email} onChange={event => setEmail(event.target.value)} />
                    <button
                        type="button"
                        className="button"
                        disabled={busy || !email.includes('@')}
                        onClick={() => void run(async () => {
                            await api.sendEmailCode(email.trim());
                            setSent(true);
                        })}
                    >{sent ? '重新发送' : '发送验证码'}</button>
                </div>
            </Field>
            {sent ? (
                <>
                    <Notice kind="ok">验证码已发送，10 分钟内有效。没收到的话看看垃圾邮件。</Notice>
                    <Field label="验证码">
                        <input autoFocus required inputMode="numeric" maxLength={6} autoComplete="one-time-code" value={code} onChange={event => setCode(event.target.value)} />
                    </Field>
                </>
            ) : null}
            <Notice>{error}</Notice>
            {sent ? <button className="button is-primary is-wide" disabled={busy}>{busy ? '正在验证…' : '验证邮箱'}</button> : null}
        </form>
    );
};

const CcwVerifier = ({onDone}: {onDone: (me: Me) => void}) => {
    const [challenge, setChallenge] = useState<CcwChallenge | null>(null);
    const [waiting, setWaiting] = useState('');
    const {busy, error, run} = useSubmit();
    if (!challenge) {
        return (
            <div>
                <p className="step">在 CCW 指定作品的评论区发一串口令，就能确认这个 CCW 账号是你的。不需要 CCW 密码。</p>
                <Notice>{error}</Notice>
                <button type="button" className="button is-primary is-wide" disabled={busy} onClick={() => void run(async () => setChallenge(await api.ccwChallenge()))}>
                    {busy ? '正在获取…' : '获取评论口令'}
                </button>
            </div>
        );
    }
    return (
        <div>
            <p className="step">用你的 CCW 账号，把这串口令发到评论区：</p>
            <code className="passcode">{challenge.code}</code>
            {challenge.comment_url ? <a className="button is-wide" href={challenge.comment_url} target="_blank" rel="noreferrer">打开评论区</a> : null}
            <Notice kind="ok">{waiting}</Notice>
            <Notice>{error}</Notice>
            <button
                type="button"
                className="button is-primary is-wide"
                disabled={busy}
                onClick={() => void run(async () => {
                    const result = await api.ccwCheck();
                    if (result.me.ccw) {
                        onDone(result.me);
                    } else if (result.challenge?.status === 'conflict') {
                        throw new Error('这个 CCW 账号已经绑定了别的账号');
                    } else if (result.challenge?.status === 'expired') {
                        setChallenge(null);
                        throw new Error('口令已过期，请重新获取');
                    } else {
                        setWaiting('还没看到这条评论。发出后等几秒再试。');
                    }
                })}
            >{busy ? '正在检查…' : '我发好了'}</button>
        </div>
    );
};

const Verify = () => {
    const [method, setMethod] = useState<'email' | 'ccw'>('email');
    const done = () => afterLogin();
    return (
        <AuthShell>
            <h1>验证账号</h1>
            <p className="lead">验证邮箱或绑定 CCW，任选其一。验证后才能登录其他服务。</p>
            <div className="segmented" role="tablist">
                <button type="button" role="tab" aria-selected={method === 'email'} onClick={() => setMethod('email')}>邮箱</button>
                <button type="button" role="tab" aria-selected={method === 'ccw'} onClick={() => setMethod('ccw')}>CCW</button>
            </div>
            {method === 'email' ? <EmailVerifier onDone={done} /> : <CcwVerifier onDone={done} />}
            {requestId ? null : <p className="switch"><a className="link" href="/account">稍后再说</a></p>}
        </AuthShell>
    );
};

// ---------------------------------------------------------------- 授权确认

const Continue = () => {
    const [info, setInfo] = useState<OAuthRequestInfo | null>(null);
    const [me, setMe] = useState<Me | null>(null);
    const [failed, setFailed] = useState('');
    const {busy, error, run} = useSubmit();

    useEffect(() => {
        if (!requestId) {
            setFailed('缺少登录请求，请回到原来的页面重新发起。');
            return;
        }
        api.oauthRequest(requestId).then(async result => {
            if (result.needs_login) return go(withRequest('/login'));
            if (result.needs_verification) return go(withRequest('/verify'));
            setMe(await api.me());
            setInfo(result);
        }).catch((caught: Error) => setFailed(caught.message));
    }, []);

    if (failed) {
        return (
            <AuthShell>
                <h1>无法继续</h1>
                <p className="lead">{failed}</p>
                <a className="button is-wide" href="/account">前往我的账号</a>
            </AuthShell>
        );
    }
    if (!info || !me) return <AuthShell><p className="lead">正在读取…</p></AuthShell>;

    const scopes = scopeList(info.scopes);
    return (
        <AuthShell>
            <h1>登录 {info.client.name}</h1>
            <p className="lead">{info.client.name} 想使用你的 KukeAuth 账号登录。</p>
            {info.client.official ? null : (
                <p className="third-party">
                    第三方应用{info.client.developer ? `，由 @${info.client.developer.username} 开发` : ''}，不是 KukeAuth 官方服务。
                    {info.client.homepage_url ? <> <a className="link" href={info.client.homepage_url} target="_blank" rel="noreferrer">应用主页</a></> : null}
                </p>
            )}
            <div className="identity">
                <Avatar url={me.avatar_url} name={me.nickname} size={44} />
                <div className="identity-text"><b>{me.nickname}</b><span>@{me.username}</span></div>
                <button
                    type="button"
                    className="link"
                    onClick={() => void run(async () => {
                        await api.logout();
                        go(withRequest('/login'));
                    })}
                >切换账号</button>
            </div>
            {scopes.length ? (
                <>
                    <p className="step">它会获得：</p>
                    <ul className="scope-list">{scopes.map(scope => <li key={scope}>{scope}</li>)}</ul>
                </>
            ) : null}
            <Notice>{error}</Notice>
            <button className="button is-primary is-wide" disabled={busy} onClick={() => void run(async () => go((await api.approve(requestId!)).redirect_url))}>继续</button>
            <button className="button is-wide is-quiet" disabled={busy} onClick={() => void run(async () => go((await api.deny(requestId!)).redirect_url))}>取消</button>
        </AuthShell>
    );
};

// ---------------------------------------------------------------- 我的账号

const ProfileTab = ({me, onChange}: {me: Me; onChange: (me: Me) => void}) => {
    const [nickname, setNickname] = useState(me.nickname);
    const [bio, setBio] = useState(me.bio ?? '');
    const [saved, setSaved] = useState('');
    const file = useRef<HTMLInputElement>(null);
    const {busy, error, run, form} = useSubmit();
    const dirty = nickname.trim() !== me.nickname || bio !== (me.bio ?? '');

    const pick = (event: ChangeEvent<HTMLInputElement>) => {
        const chosen = event.target.files?.[0];
        event.target.value = '';
        if (!chosen) return;
        void run(async () => {
            setSaved('');
            const {url} = await api.uploadAvatar(chosen);
            onChange(await api.updateProfile({avatar_url: url}));
            setSaved('头像已更新');
        });
    };

    return (
        <section>
            <h2>资料</h2>
            <p className="section-lead">这些信息会显示在你登录的所有服务上。</p>
            <div className="avatar-row">
                <Avatar url={me.avatar_url} name={me.nickname} size={72} />
                <div>
                    <button type="button" className="button" disabled={busy} onClick={() => file.current?.click()}>{busy ? '正在处理…' : '更换头像'}</button>
                    <p className="field-hint">支持 PNG、JPG、WebP。</p>
                </div>
                <input ref={file} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={pick} />
            </div>
            <form onSubmit={form(async () => {
                setSaved('');
                onChange(await api.updateProfile({nickname: nickname.trim(), bio}));
                setSaved('已保存');
            })}>
                <Field label="用户名" hint="用户名不能修改。">
                    <input disabled value={me.username} />
                </Field>
                <Field label="昵称">
                    <input required maxLength={80} value={nickname} onChange={event => setNickname(event.target.value)} />
                </Field>
                <Field label="简介" hint={`${Array.from(bio).length} / 1000`}>
                    <textarea rows={4} maxLength={1000} value={bio} onChange={event => setBio(event.target.value)} />
                </Field>
                <Notice>{error}</Notice>
                <Notice kind="ok">{saved}</Notice>
                <button className="button is-primary" disabled={busy || !dirty}>保存</button>
            </form>
        </section>
    );
};

const PasswordForm = () => {
    const [current, setCurrent] = useState('');
    const [next, setNext] = useState('');
    const [saved, setSaved] = useState('');
    const {busy, error, form} = useSubmit();
    return (
        <form onSubmit={form(async () => {
            setSaved('');
            await api.changePassword(current, next);
            setCurrent('');
            setNext('');
            setSaved('密码已修改，其他设备已退出登录。');
        })}>
            <Field label="当前密码">
                <input required type="password" autoComplete="current-password" value={current} onChange={event => setCurrent(event.target.value)} />
            </Field>
            <Field label="新密码" hint="至少 8 位。修改后，其他设备和 KukeChat 网页端会退出登录。">
                <input required minLength={8} type="password" autoComplete="new-password" value={next} onChange={event => setNext(event.target.value)} />
            </Field>
            <Notice>{error}</Notice>
            <Notice kind="ok">{saved}</Notice>
            <button className="button is-primary" disabled={busy}>修改密码</button>
        </form>
    );
};

const SecurityTab = ({me, onChange}: {me: Me; onChange: (me: Me) => void}) => {
    const [editing, setEditing] = useState(false);
    const done = (next: Me) => {
        onChange(next);
        setEditing(false);
    };
    return (
        <section>
            <h2>安全</h2>
            <p className="section-lead">验证邮箱后，可以用它找回密码和登录其他服务。</p>
            <div className="rows">
                <div className="row">
                    <div><b>邮箱</b><span>{me.email ?? '未验证'}</span></div>
                    <button type="button" className="button" onClick={() => setEditing(!editing)}>
                        {editing ? '收起' : me.email ? '更换' : '验证'}
                    </button>
                </div>
                {editing ? <div className="row-body"><EmailVerifier onDone={done} /></div> : null}
            </div>
            <h3>修改密码</h3>
            <PasswordForm />
        </section>
    );
};

const UnbindForm = ({item, onDone}: {item: Connection; onDone: (items: Connection[], me: Me) => void}) => {
    const [password, setPassword] = useState('');
    const {busy, error, form} = useSubmit();
    return (
        <form onSubmit={form(async () => {
            const result = await api.unbindConnection(item.provider, password);
            onDone(result.items, result.me);
        })}>
            <p className="step">解绑后，这个账号不能再用 {item.label} 找回密码，接入的服务也读不到它。KukeChat 里的金币、陪审资格和绑定奖励都要绑定 CCW 才有。</p>
            <Field label="当前密码">
                <input autoFocus required type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} />
            </Field>
            <Notice>{error}</Notice>
            <button className="button is-primary" disabled={busy}>{busy ? '正在解绑…' : `解绑 ${item.label}`}</button>
        </form>
    );
};

// 每种外部账号怎么绑定。接入新的只要在这里加一项
const BINDERS: Record<string, (props: {onDone: (me: Me) => void}) => JSX.Element> = {ccw: CcwVerifier};

const ConnectionsTab = ({onChange}: {onChange: (me: Me) => void}) => {
    const [items, setItems] = useState<Connection[] | null>(null);
    const [open, setOpen] = useState<string | null>(null);
    const [failed, setFailed] = useState('');

    useEffect(() => {
        api.connections().then(result => setItems(result.items)).catch((caught: Error) => setFailed(caught.message));
    }, []);

    return (
        <section>
            <h2>账号绑定</h2>
            <p className="section-lead">绑定外部账号可以确认身份和找回密码。一个外部账号只能绑定一个 KukeAuth 账号。</p>
            <Notice>{failed}</Notice>
            {items === null && !failed ? <p className="empty">正在读取…</p> : null}
            <div className="rows">
                {items?.map(item => {
                    const Binder = BINDERS[item.provider];
                    const expanded = open === item.provider;
                    return (
                        <div key={item.provider}>
                            <div className="row">
                                <div className="connection">
                                    {item.account ? <Avatar url={item.account.avatar_url} name={item.account.name ?? item.label} size={40} /> : null}
                                    <div>
                                        <b>{item.label}</b>
                                        {item.account ? (
                                            <a className="link" href={item.account.url} target="_blank" rel="noreferrer">{item.account.name ?? '查看主页'}</a>
                                        ) : <span>{item.available ? '未绑定' : '暂未开放'}</span>}
                                    </div>
                                </div>
                                {item.connected || item.available ? (
                                    <button type="button" className="button" onClick={() => setOpen(expanded ? null : item.provider)}>
                                        {expanded ? '收起' : item.connected ? '解绑' : '绑定'}
                                    </button>
                                ) : null}
                            </div>
                            {expanded ? (
                                <div className="row-body">
                                    {!item.connected ? <Binder onDone={next => {
                                        onChange(next);
                                        setOpen(null);
                                        void api.connections().then(result => setItems(result.items)).catch((caught: Error) => setFailed(caught.message));
                                    }} /> : item.can_unbind ? <UnbindForm item={item} onDone={(nextItems, next) => {
                                        setItems(nextItems);
                                        onChange(next);
                                        setOpen(null);
                                    }} /> : <p className="step">这是你唯一的验证方式。请先到「安全」验证邮箱，再解绑，否则账号将无法登录其他服务和找回密码。</p>}
                                </div>
                            ) : null}
                        </div>
                    );
                })}
            </div>
        </section>
    );
};

const AppsTab = () => {
    const [items, setItems] = useState<Grant[] | null>(null);
    const {busy, error, setError, run} = useSubmit();
    useEffect(() => {
        api.grants().then(result => setItems(result.items)).catch((caught: Error) => setError(caught.message));
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    return (
        <section>
            <h2>已授权应用</h2>
            <p className="section-lead">这些服务可以通过 KukeAuth 确认你的身份。取消后，下次登录时会重新询问。</p>
            <Notice>{error}</Notice>
            {items === null ? <p className="empty">{error ? '' : '正在读取…'}</p> : null}
            {items?.length === 0 ? <p className="empty">还没有授权过任何服务。</p> : null}
            <div className="rows">
                {items?.map(item => (
                    <div className="row" key={item.client_id}>
                        <div>
                            <b>{item.name}{item.official ? null : <em className="tag">第三方</em>}</b>
                            <span>{scopeList(item.scopes).join('、') || '仅确认身份'}</span>
                        </div>
                        <button
                            type="button"
                            className="button"
                            disabled={busy}
                            onClick={() => void run(async () => {
                                await api.revokeGrant(item.client_id);
                                setItems(items.filter(other => other.client_id !== item.client_id));
                            })}
                        >取消授权</button>
                    </div>
                ))}
            </div>
        </section>
    );
};

type Tab = 'profile' | 'security' | 'connections' | 'apps' | 'developer';
const TABS: [Tab, string][] = [['profile', '资料'], ['security', '安全'], ['connections', '账号绑定'], ['apps', '已授权应用'], ['developer', '开发者']];
const initialTab = (): Tab => (TABS.find(([key]) => key === query.get('tab'))?.[0] ?? 'profile');

const Account = () => {
    const [me, setMe] = useState<Me | null>(null);
    const [tab, setTab] = useState<Tab>(initialTab);
    const [failed, setFailed] = useState('');

    useEffect(() => {
        api.me().then(setMe).catch((caught: unknown) => {
            if (caught instanceof ApiError && caught.status === 401) go(tab === 'profile' ? '/login' : `/login?next=${encodeURIComponent(`/account?tab=${tab}`)}`);
            else setFailed(caught instanceof Error ? caught.message : '读取失败');
        });
    }, []);

    if (failed) return <div className="center"><Notice>{failed}</Notice></div>;
    if (!me) return <div className="center"><p className="empty">正在读取…</p></div>;

    return (
        <div className="account">
            <header className="topbar">
                <Wordmark href="/account" />
                <div className="topbar-side">
                    <a className="top-link" href="/docs">开发者文档</a>
                    <ThemeToggle />
                    <button type="button" className="button is-quiet" onClick={() => void api.logout().then(() => go('/login'))}>退出登录</button>
                </div>
            </header>
            <div className="account-hero">
                <Avatar url={me.avatar_url} name={me.nickname} size={88} />
                <div className="account-who">
                    <h1>{me.nickname}</h1>
                    <p>@{me.username}</p>
                </div>
                <span className={`badge${me.verified ? ' is-solid' : ''}`}>{me.verified ? '已验证' : '未验证'}</span>
            </div>
            {me.verified ? null : (
                <a className="banner" href="/verify"><b>账号还没验证</b><span>验证邮箱或绑定 CCW 后，才能登录其他服务。去验证 →</span></a>
            )}
            <div className="account-body">
                <nav className="tabs" role="tablist">
                    {TABS.map(([key, label]) => (
                        <button key={key} type="button" role="tab" aria-selected={tab === key} onClick={() => {
                            setTab(key);
                            history.replaceState(null, '', key === 'profile' ? '/account' : `/account?tab=${key}`);
                        }}>{label}</button>
                    ))}
                </nav>
                <div className="tab-body">
                    {tab === 'profile' ? <ProfileTab me={me} onChange={setMe} /> : null}
                    {tab === 'security' ? <SecurityTab me={me} onChange={setMe} /> : null}
                    {tab === 'connections' ? <ConnectionsTab onChange={setMe} /> : null}
                    {tab === 'apps' ? <AppsTab /> : null}
                    {tab === 'developer' ? <DeveloperTab /> : null}
                </div>
            </div>
        </div>
    );
};

const Home = () => {
    useEffect(() => {
        api.me().then(() => go('/account')).catch(() => go('/login'));
    }, []);
    return <div className="center"><p className="empty">正在读取…</p></div>;
};

export const App = () => {
    switch (location.pathname.replace(/\/+$/, '')) {
    case '/login': return <Login />;
    case '/register': return <Register />;
    case '/forgot': return <Forgot />;
    case '/verify': return <Verify />;
    case '/continue': return <Continue />;
    case '/account': return <Account />;
    case '/docs': return <Docs />;
    default: return <Home />;
    }
};
