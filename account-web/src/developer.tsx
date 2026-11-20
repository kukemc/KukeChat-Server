import {useEffect, useState} from 'react';
import {api, type AppFields, type DeveloperApp} from './api';
import {CopyButton, Field, Notice, useSubmit} from './ui';

const SCOPES: [string, string][] = [
    ['openid', '确认身份（sub）'],
    ['profile', '用户名、昵称、头像和简介'],
    ['email', '已验证的邮箱地址'],
    ['ccw', '绑定的 CCW 账号'],
    ['offline_access', '刷新令牌，用户离开后保持登录']
];

const lines = (text: string): string[] => text.split('\n').map(line => line.trim()).filter(Boolean);

const AppForm = ({app, submitLabel, onSubmit, onCancel}: {
    app?: DeveloperApp;
    submitLabel: string;
    onSubmit: (fields: AppFields, isPublic: boolean) => Promise<void>;
    onCancel: () => void;
}) => {
    const [name, setName] = useState(app?.name ?? '');
    const [homepage, setHomepage] = useState(app?.homepage_url ?? '');
    const [icon, setIcon] = useState(app?.icon_url ?? '');
    const [redirects, setRedirects] = useState((app?.redirect_uris ?? []).join('\n'));
    const [logouts, setLogouts] = useState((app?.post_logout_redirect_uris ?? []).join('\n'));
    const [scopes, setScopes] = useState<string[]>(app?.allowed_scopes ?? ['openid', 'profile']);
    const [isPublic, setPublic] = useState(app?.public ?? false);
    const {busy, error, form} = useSubmit();

    const toggle = (scope: string) => setScopes(scopes.includes(scope) ? scopes.filter(item => item !== scope) : [...scopes, scope]);

    return (
        <form onSubmit={form(async () => {
            await onSubmit({
                name: name.trim(),
                homepage_url: homepage.trim() || null,
                icon_url: icon.trim() || null,
                redirect_uris: lines(redirects),
                post_logout_redirect_uris: lines(logouts),
                allowed_scopes: SCOPES.map(([scope]) => scope).filter(scope => scopes.includes(scope))
            }, isPublic);
        })}>
            <Field label="应用名称" hint="用户在授权页上看到的名字。">
                <input required maxLength={80} value={name} onChange={event => setName(event.target.value)} />
            </Field>
            <Field label="应用主页" hint="选填，https 地址。授权页上会附上链接。">
                <input type="url" maxLength={500} placeholder="https://" value={homepage} onChange={event => setHomepage(event.target.value)} />
            </Field>
            <Field label="图标地址" hint="选填，https 地址，建议正方形。">
                <input type="url" maxLength={500} placeholder="https://" value={icon} onChange={event => setIcon(event.target.value)} />
            </Field>
            <Field label="回调地址" hint="每行一个，最多 10 个。必须是 https，或本机调试用的 http://127.0.0.1。授权时要求完全相同。">
                <textarea required rows={3} className="mono" spellCheck={false} placeholder="https://example.com/auth/callback" value={redirects} onChange={event => setRedirects(event.target.value)} />
            </Field>
            <Field label="登出后跳转地址" hint="选填，每行一个。调用登出接口时 post_logout_redirect_uri 只能是这里的地址。">
                <textarea rows={2} className="mono" spellCheck={false} value={logouts} onChange={event => setLogouts(event.target.value)} />
            </Field>
            <fieldset className="field choices">
                <legend className="field-label">可以申请的权限（scope）</legend>
                {SCOPES.map(([scope, label]) => (
                    <label key={scope} className="choice">
                        <input type="checkbox" checked={scopes.includes(scope)} onChange={() => toggle(scope)} />
                        <span><code>{scope}</code>{label}</span>
                    </label>
                ))}
            </fieldset>
            {app ? null : (
                <fieldset className="field choices">
                    <legend className="field-label">应用类型</legend>
                    <label className="choice">
                        <input type="radio" name="kind" checked={!isPublic} onChange={() => setPublic(false)} />
                        <span><b>有后端的应用</b>能安全保存 client secret，换令牌时要带上它。网站一般选这个。</span>
                    </label>
                    <label className="choice">
                        <input type="radio" name="kind" checked={isPublic} onChange={() => setPublic(true)} />
                        <span><b>公开应用</b>桌面程序、手机 App 这类保存不了 secret 的原生程序，只靠 PKCE。纯网页前端不支持，需要一个后端。创建后不能更改类型。</span>
                    </label>
                </fieldset>
            )}
            <Notice>{error}</Notice>
            <div className="actions">
                <button className="button is-primary" disabled={busy}>{busy ? '正在保存…' : submitLabel}</button>
                <button type="button" className="button is-quiet" disabled={busy} onClick={onCancel}>取消</button>
            </div>
        </form>
    );
};

const SecretBox = ({clientId, secret, onClose}: {clientId: string; secret: string | null; onClose: () => void}) => (
    <div className="secret-box">
        <b>{secret ? '请立即保存 client secret' : '应用已创建'}</b>
        <p>{secret ? '它只显示这一次，KukeAuth 只保存它的哈希。丢了只能重置。' : '公开应用没有 client secret，换令牌时只带 client_id 和 PKCE。'}</p>
        <div className="kv">
            <span>client_id</span>
            <code>{clientId}</code>
            <CopyButton text={clientId} />
        </div>
        {secret ? (
            <div className="kv">
                <span>client_secret</span>
                <code>{secret}</code>
                <CopyButton text={secret} />
            </div>
        ) : null}
        <button type="button" className="button" onClick={onClose}>我已保存</button>
    </div>
);

const AppDetail = ({app, onSaved, onDeleted}: {app: DeveloperApp; onSaved: (app: DeveloperApp) => void; onDeleted: () => void}) => {
    const [editing, setEditing] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const [secret, setSecret] = useState<string | null>(null);
    const {busy, error, run} = useSubmit();

    if (editing) {
        return (
            <AppForm
                app={app}
                submitLabel="保存"
                onCancel={() => setEditing(false)}
                onSubmit={async fields => {
                    onSaved(await api.updateApp(app.client_id, fields));
                    setEditing(false);
                }}
            />
        );
    }
    return (
        <div>
            {app.disabled ? <Notice>这个应用已被管理员停用，用户无法通过它登录。如有疑问请联系管理员。</Notice> : null}
            {secret ? <SecretBox clientId={app.client_id} secret={secret} onClose={() => setSecret(null)} /> : null}
            <dl className="facts">
                <dt>client_id</dt>
                <dd><code>{app.client_id}</code> <CopyButton text={app.client_id} /></dd>
                <dt>类型</dt>
                <dd>{app.public ? '公开应用（只靠 PKCE）' : '有后端的应用（client secret + PKCE）'}</dd>
                <dt>回调地址</dt>
                <dd>{app.redirect_uris.map(uri => <code key={uri} className="block">{uri}</code>)}</dd>
                {app.post_logout_redirect_uris.length ? (
                    <>
                        <dt>登出后跳转</dt>
                        <dd>{app.post_logout_redirect_uris.map(uri => <code key={uri} className="block">{uri}</code>)}</dd>
                    </>
                ) : null}
                <dt>scope</dt>
                <dd><code>{app.allowed_scopes.join(' ')}</code></dd>
                {app.homepage_url ? (
                    <>
                        <dt>主页</dt>
                        <dd><a className="link" href={app.homepage_url} target="_blank" rel="noreferrer">{app.homepage_url}</a></dd>
                    </>
                ) : null}
            </dl>
            <Notice>{error}</Notice>
            <div className="actions">
                <button type="button" className="button" onClick={() => setEditing(true)}>编辑</button>
                {app.public ? null : (
                    <button type="button" className="button" disabled={busy} onClick={() => void run(async () => setSecret((await api.rotateAppSecret(app.client_id)).client_secret))}>
                        重置 secret
                    </button>
                )}
                {confirmDelete ? (
                    <>
                        <button type="button" className="button is-primary" disabled={busy} onClick={() => void run(async () => {
                            await api.deleteApp(app.client_id);
                            onDeleted();
                        })}>确认删除</button>
                        <button type="button" className="button is-quiet" onClick={() => setConfirmDelete(false)}>取消</button>
                    </>
                ) : (
                    <button type="button" className="button is-quiet" onClick={() => setConfirmDelete(true)}>删除应用</button>
                )}
            </div>
            {confirmDelete ? <p className="field-hint">删除后所有用户的授权和登录状态立即失效，不能恢复。</p> : null}
        </div>
    );
};

export const DeveloperTab = () => {
    const [apps, setApps] = useState<DeveloperApp[] | null>(null);
    const [limit, setLimit] = useState(10);
    const [canCreate, setCanCreate] = useState(false);
    const [creating, setCreating] = useState(false);
    const [open, setOpen] = useState<string | null>(null);
    const [created, setCreated] = useState<{clientId: string; secret: string | null} | null>(null);
    const [failed, setFailed] = useState('');

    useEffect(() => {
        api.apps().then(result => {
            setApps(result.items);
            setLimit(result.limit);
            setCanCreate(result.can_create);
        }).catch((caught: Error) => setFailed(caught.message));
    }, []);

    const replace = (next: DeveloperApp) => setApps((apps ?? []).map(item => (item.client_id === next.client_id ? next : item)));

    return (
        <section>
            <h2>开发者</h2>
            <p className="section-lead">
                创建应用后，你的网站或程序就能用 KukeAuth 账号登录。接入方法见<a className="link" href="/docs">开发者文档</a>。
            </p>
            <Notice>{failed}</Notice>
            {created ? <SecretBox clientId={created.clientId} secret={created.secret} onClose={() => setCreated(null)} /> : null}
            {creating ? (
                <div className="panel">
                    <h3 className="panel-title">新建应用</h3>
                    <AppForm
                        submitLabel="创建应用"
                        onCancel={() => setCreating(false)}
                        onSubmit={async (fields, isPublic) => {
                            const result = await api.createApp({...fields, public: isPublic});
                            const {client_secret: secret, ...app} = result;
                            setApps([...(apps ?? []), app]);
                            setCreated({clientId: app.client_id, secret});
                            setOpen(app.client_id);
                            setCreating(false);
                        }}
                    />
                </div>
            ) : apps ? (
                canCreate ? (
                    <button type="button" className="button is-primary" disabled={apps.length >= limit} onClick={() => setCreating(true)}>
                        {apps.length >= limit ? `已达上限（${limit} 个）` : '新建应用'}
                    </button>
                ) : (
                    <a className="banner" href="/verify"><b>先验证账号</b><span>验证邮箱或绑定 CCW 后才能创建应用。去验证 →</span></a>
                )
            ) : null}
            {apps === null && !failed ? <p className="empty">正在读取…</p> : null}
            {apps?.length ? (
                <div className="rows spaced">
                    {apps.map(app => {
                        const expanded = open === app.client_id;
                        return (
                            <div key={app.client_id}>
                                <div className="row">
                                    <div>
                                        <b>{app.name}{app.disabled ? <em className="tag">已停用</em> : null}</b>
                                        <span className="mono">{app.client_id}</span>
                                    </div>
                                    <button type="button" className="button" onClick={() => setOpen(expanded ? null : app.client_id)}>{expanded ? '收起' : '管理'}</button>
                                </div>
                                {expanded ? (
                                    <div className="row-body">
                                        <AppDetail
                                            app={app}
                                            onSaved={replace}
                                            onDeleted={() => {
                                                setApps(apps.filter(item => item.client_id !== app.client_id));
                                                setOpen(null);
                                                if (created?.clientId === app.client_id) setCreated(null);
                                            }}
                                        />
                                    </div>
                                ) : null}
                            </div>
                        );
                    })}
                </div>
            ) : null}
        </section>
    );
};
