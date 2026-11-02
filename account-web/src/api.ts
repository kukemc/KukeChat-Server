export interface Me {
    id: string;
    username: string;
    nickname: string;
    avatar_url: string | null;
    bio: string | null;
    email: string | null;
    ccw: {name: string | null; avatar_url: string | null} | null;
    verified: boolean;
}

export interface OAuthRequestInfo {
    client: {name: string; icon_url: string | null};
    scopes: string[];
    needs_login: boolean;
    needs_verification: boolean;
}

export interface CcwChallenge {
    code: string;
    status: string;
    comment_url: string | null;
    expires_at: string;
    matched_name: string | null;
}

export interface Grant {
    client_id: string;
    name: string;
    icon_url: string | null;
    scopes: string[];
    created_at: string;
}

export interface Connection {
    provider: string;
    label: string;
    connected: boolean;
    available: boolean;
    account: {name: string | null; avatar_url: string | null; url: string} | null;
    can_unbind: boolean;
}

export class ApiError extends Error {
    constructor(readonly status: number, readonly code: string, message: string, readonly detail?: Record<string, unknown>) {
        super(message);
    }
}

// 后端里沿用 KukeChat 的英文错误，换成给人看的话
const MESSAGES: Record<string, string> = {
    'This IP address has already registered an account': '这个网络已经注册过账号了。每个网络只能注册一个，可以换个网络再试。',
    'Username or email already exists': '这个用户名已经被占用了'
};

const DEVICE_KEY = 'kukeauth-device';

const storageGet = (key: string): string | null => {
    try {
        return localStorage.getItem(key);
    } catch {
        return null;
    }
};

const storageSet = (key: string, value: string): void => {
    try {
        localStorage.setItem(key, value);
    } catch {
        // 隐私模式下存不了，下次重新领
    }
};

const sha256 = async (text: string): Promise<string | null> => {
    if (!crypto.subtle) return null;
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
};

let fingerprint: Promise<string | null> | null = null;

// 防小号用的设备信号（docs/device-bans.md），缺了也能注册
const getFingerprint = (): Promise<string | null> => {
    fingerprint ??= sha256([
        screen.width, screen.height, screen.colorDepth,
        Intl.DateTimeFormat().resolvedOptions().timeZone,
        navigator.language, navigator.hardwareConcurrency
    ].join('|'));
    return fingerprint;
};

const deviceHeaders = async (): Promise<Record<string, string>> => {
    const headers: Record<string, string> = {'X-Kuke-Platform': 'web'};
    let device = storageGet(DEVICE_KEY);
    if (!device) {
        try {
            const response = await fetch('/account/api/device', {credentials: 'same-origin'});
            device = ((await response.json()) as {device: string}).device;
            storageSet(DEVICE_KEY, device);
        } catch {
            device = null;
        }
    }
    if (device) headers['X-Kuke-Device'] = device;
    const fp = await getFingerprint();
    if (fp) headers['X-Kuke-Fp'] = fp;
    return headers;
};

const request = async <T>(path: string, init: RequestInit = {}, withDevice = false): Promise<T> => {
    const headers: Record<string, string> = withDevice ? await deviceHeaders() : {};
    if (init.body && !(init.body instanceof FormData)) headers['Content-Type'] = 'application/json';
    let response: Response;
    try {
        response = await fetch(`/account/api${path}`, {credentials: 'same-origin', ...init, headers});
    } catch {
        throw new ApiError(0, 'network', '连不上服务器，请检查网络后重试');
    }
    const body: unknown = await response.json().catch(() => null);
    if (response.ok) return body as T;
    const detail = (body as {detail?: unknown} | null)?.detail;
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
        const info = detail as {code?: string; message?: string};
        throw new ApiError(response.status, info.code ?? 'error', info.message ?? '操作失败，请重试', detail as Record<string, unknown>);
    }
    if (typeof detail === 'string') throw new ApiError(response.status, 'error', MESSAGES[detail] ?? detail);
    if (response.status === 422) throw new ApiError(422, 'invalid', '填写的内容不符合要求，请检查后重试');
    throw new ApiError(response.status, 'error', '操作失败，请重试');
};

const post = <T>(path: string, body?: unknown, withDevice = false): Promise<T> =>
    request<T>(path, {method: 'POST', body: body === undefined ? undefined : JSON.stringify(body)}, withDevice);

export const api = {
    me: () => request<Me>('/me'),
    login: (username: string, password: string) => post<Me>('/login', {username, password}, true),
    register: (username: string, nickname: string, password: string) => post<Me>('/register', {username, nickname, password}, true),
    logout: () => post<{ok: boolean}>('/logout'),
    updateProfile: (changes: {nickname?: string; bio?: string; avatar_url?: string}) =>
        request<Me>('/me', {method: 'PATCH', body: JSON.stringify(changes)}),
    uploadAvatar: (file: File) => {
        const body = new FormData();
        body.append('file', file);
        return request<{url: string}>('/me/avatar', {method: 'POST', body});
    },
    changePassword: (current_password: string, new_password: string) => post<{ok: boolean}>('/me/password', {current_password, new_password}),
    grants: () => request<{items: Grant[]}>('/me/grants'),
    revokeGrant: (clientId: string) => request<{ok: boolean}>(`/me/grants/${encodeURIComponent(clientId)}`, {method: 'DELETE'}),
    sendEmailCode: (email: string) => post<{ok: boolean}>('/email/send', {email}),
    verifyEmail: (email: string, code: string) => post<Me>('/email/verify', {email, code}),
    ccwChallenge: () => post<CcwChallenge>('/ccw/challenge'),
    ccwCheck: () => post<{challenge: CcwChallenge | null; me: Me}>('/ccw/check'),
    connections: () => request<{items: Connection[]}>('/connections'),
    unbindConnection: (provider: string, password: string) => post<{items: Connection[]; me: Me}>(`/connections/${encodeURIComponent(provider)}/unbind`, {password}),
    resetEmailSend: (email: string) => post<{ok: boolean}>('/password-reset/email/send', {email}),
    resetEmailConfirm: (email: string, code: string, new_password: string) => post<{ok: boolean}>('/password-reset/email/confirm', {email, code, new_password}),
    resetCcwChallenge: (username: string) => post<{code: string; comment_url: string | null}>('/password-reset/ccw/challenge', {username}),
    resetCcwConfirm: (username: string, code: string, new_password: string) => post<{ok: boolean}>('/password-reset/ccw/confirm', {username, code, new_password}),
    oauthRequest: (id: string) => request<OAuthRequestInfo>(`/requests/${encodeURIComponent(id)}`),
    approve: (id: string) => post<{redirect_url: string}>(`/requests/${encodeURIComponent(id)}/approve`),
    deny: (id: string) => post<{redirect_url: string}>(`/requests/${encodeURIComponent(id)}/deny`)
};
