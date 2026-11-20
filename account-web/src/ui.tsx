import {useEffect, useState, type FormEvent, type ReactNode} from 'react';

export type Theme = 'light' | 'dark';

const systemTheme = (): Theme => (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');

export const ThemeToggle = () => {
    const [theme, setTheme] = useState<Theme>(() => (document.documentElement.dataset.theme as Theme | undefined) ?? systemTheme());
    useEffect(() => {
        // 没手动选过时跟着系统变
        const media = matchMedia('(prefers-color-scheme: dark)');
        const onChange = () => {
            if (!document.documentElement.dataset.theme) setTheme(systemTheme());
        };
        media.addEventListener('change', onChange);
        return () => media.removeEventListener('change', onChange);
    }, []);
    const next: Theme = theme === 'dark' ? 'light' : 'dark';
    const label = next === 'dark' ? '切换到深色模式' : '切换到浅色模式';
    return (
        <button
            type="button"
            className="icon-button"
            aria-label={label}
            title={label}
            onClick={() => {
                document.documentElement.dataset.theme = next;
                try {
                    localStorage.setItem('kukeauth-theme', next);
                } catch {
                    // 存不了就只在这次生效
                }
                setTheme(next);
            }}
        >
            {theme === 'dark' ? (
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
            ) : (
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" /></svg>
            )}
        </button>
    );
};

export const Wordmark = ({href = '/'}: {href?: string}) => (
    <a className="wordmark" href={href}>Kuke<b>Auth</b></a>
);

export const Field = ({label, hint, children}: {label: string; hint?: ReactNode; children: ReactNode}) => (
    <label className="field">
        <span className="field-label">{label}</span>
        {children}
        {hint ? <span className="field-hint">{hint}</span> : null}
    </label>
);

export const Notice = ({kind = 'error', children}: {kind?: 'error' | 'ok'; children: ReactNode}) =>
    children ? <p className={`notice is-${kind}`} role={kind === 'error' ? 'alert' : 'status'}>{children}</p> : null;

export const Avatar = ({url, name, size = 40}: {url: string | null; name: string; size?: number}) => (
    <span className="avatar" style={{width: size, height: size, fontSize: size * 0.42}}>
        {url ? <img src={url} alt="" /> : Array.from(name)[0]?.toUpperCase()}
    </span>
);

export const CopyButton = ({text, label = '复制'}: {text: string; label?: string}) => {
    const [done, setDone] = useState(false);
    return (
        <button
            type="button"
            className="copy-button"
            onClick={() => {
                void navigator.clipboard?.writeText(text).then(() => {
                    setDone(true);
                    setTimeout(() => setDone(false), 1500);
                });
            }}
        >{done ? '已复制' : label}</button>
    );
};

/** 提交时统一处理 loading 和错误，页面只写真正要做的事。 */
export const useSubmit = () => {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const run = async (task: () => Promise<void>) => {
        if (busy) return;
        setBusy(true);
        setError('');
        try {
            await task();
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : '操作失败，请重试');
        } finally {
            setBusy(false);
        }
    };
    const form = (task: () => Promise<void>) => (event: FormEvent) => {
        event.preventDefault();
        void run(task);
    };
    return {busy, error, setError, run, form};
};

/** 登录、注册这些页面的外壳：一半大字，一半表单。 */
export const AuthShell = ({children}: {children: ReactNode}) => (
    <div className="auth">
        <aside className="auth-panel">
            <Wordmark />
            <svg className="auth-mark" viewBox="0 0 400 400" aria-hidden="true">
                <path d="M60 20h78v150L276 20h98L222 182l164 198h-100L138 196v184H60Z" />
            </svg>
            <div className="auth-slogan">
                <h2>一个账号，<br />通行所有服务。</h2>
                <p>KukeChat、Gandi 反馈站，以及之后的每一个新服务。</p>
            </div>
        </aside>
        <main className="auth-main">
            <div className="auth-top">
                <span className="auth-top-mark"><Wordmark /></span>
                <a className="top-link" href="/docs">开发者文档</a>
                <ThemeToggle />
            </div>
            <div className="auth-form">{children}</div>
        </main>
    </div>
);
