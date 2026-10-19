
#define WIN32_LEAN_AND_MEAN
#define COBJMACROS
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif

#include <windows.h>
#include <initguid.h>
#include <mmdeviceapi.h>
#include <audiopolicy.h>
#include <endpointvolume.h>
#include <netfw.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define AM_EXPORT __declspec(dllexport)

#define AM_PATH_MAX   520
#define AM_CAP_CAMERA   0
#define AM_CAP_MIC      1
#define AM_CAP_LOCATION 2
#define AM_CAP_FILES    3
#define AM_CAP_SCREEN   4
#define AM_CAP_INPUT    5
#define AM_CAP_COUNT    6

#define AM_ERR_BAD_ARG     0x0EA00001
#define AM_ERR_NO_SESSION  0x0EA00002
#define AM_ERR_PATH_LONG   0x0EA00003

typedef struct AmApp {
    wchar_t   path[AM_PATH_MAX];
    int       perm[AM_CAP_COUNT];
    int       in_use[AM_CAP_COUNT];
    int       audio;
    int       net;
    int       clip;
    long long last_start[AM_CAP_COUNT];
    long long last_stop[AM_CAP_COUNT];
} AmApp;

static const wchar_t *const CAP_KEY[AM_CAP_COUNT] = {
    L"webcam",
    L"microphone",
    L"location",
    L"broadFileSystemAccess",
    L"graphicsCaptureProgrammatic",
    L"humanInterfaceDevice",
};
static const wchar_t CONSENT_ROOT[] =
    L"Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore";
static const wchar_t MANAGED_KEY[] = L"Software\\AccessMonitor\\Managed";

static AmApp *g_apps  = NULL;
static int    g_count = 0;
static int    g_cap   = 0;

static void list_clear(void)
{
    free(g_apps);
    g_apps  = NULL;
    g_count = 0;
    g_cap   = 0;
}

static AmApp *list_find(const wchar_t *path)
{
    for (int i = 0; i < g_count; ++i)
        if (_wcsicmp(g_apps[i].path, path) == 0) return &g_apps[i];
    return NULL;
}

static AmApp *list_add(const wchar_t *path)
{
    AmApp *a = list_find(path);
    if (a) return a;
    if (wcslen(path) >= AM_PATH_MAX) return NULL;

    if (g_count == g_cap) {
        int ncap = g_cap ? g_cap * 2 : 64;
        AmApp *n = (AmApp *)realloc(g_apps, sizeof(AmApp) * (size_t)ncap);
        if (!n) return NULL;
        g_apps = n;
        g_cap  = ncap;
    }
    a = &g_apps[g_count++];
    memset(a, 0, sizeof(*a));
    wcsncpy(a->path, path, AM_PATH_MAX - 1);
    for (int c = 0; c < AM_CAP_COUNT; ++c) {
        a->perm[c] = -1; a->in_use[c] = 0; a->last_start[c] = 0; a->last_stop[c] = 0;
    }
    a->audio = -2;
    a->net   = 0;
    a->clip  = 0;
    return a;
}

static int ends_with_exe(const wchar_t *p)
{
    size_t n = wcslen(p);
    return n >= 4 && _wcsicmp(p + n - 4, L".exe") == 0;
}

static void encode_path(const wchar_t *path, wchar_t *out, size_t n)
{
    size_t i = 0;
    for (; path[i] && i + 1 < n; ++i) out[i] = (path[i] == L'\\') ? L'#' : path[i];
    out[i] = 0;
}

static void decode_name(const wchar_t *name, wchar_t *out, size_t n)
{
    size_t i = 0;
    for (; name[i] && i + 1 < n; ++i) out[i] = (name[i] == L'#') ? L'\\' : name[i];
    out[i] = 0;
}

static int cap_key_path(int cap, const wchar_t *app_path, wchar_t *out, size_t n)
{
    if (cap < 0 || cap >= AM_CAP_COUNT) return AM_ERR_BAD_ARG;
    if (!app_path) {
        _snwprintf(out, n, L"%ls\\%ls\\NonPackaged", CONSENT_ROOT, CAP_KEY[cap]);
    } else {
        size_t len = wcslen(app_path);
        if (len == 0) return AM_ERR_BAD_ARG;
        if (len >= AM_PATH_MAX || len > 255) return AM_ERR_PATH_LONG;
        wchar_t enc[AM_PATH_MAX];
        encode_path(app_path, enc, AM_PATH_MAX);
        _snwprintf(out, n, L"%ls\\%ls\\NonPackaged\\%ls", CONSENT_ROOT, CAP_KEY[cap], enc);
    }
    out[n - 1] = 0;
    return 0;
}

static int read_consent_value(const wchar_t *subkey)
{
    HKEY k;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, subkey, 0, KEY_READ, &k) != ERROR_SUCCESS) return -1;

    wchar_t buf[32] = {0};
    DWORD   size = sizeof(buf) - sizeof(wchar_t);
    DWORD   type = 0;
    LONG    r = RegQueryValueExW(k, L"Value", NULL, &type, (LPBYTE)buf, &size);
    RegCloseKey(k);
    if (r != ERROR_SUCCESS || type != REG_SZ) return -1;

    if (_wcsicmp(buf, L"Allow") == 0) return 1;
    if (_wcsicmp(buf, L"Deny") == 0)  return 0;
    return -1;
}

static void read_last_used(const wchar_t *subkey, ULONGLONG *start, ULONGLONG *stop)
{
    *start = 0;
    *stop  = 0;
    HKEY k;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, subkey, 0, KEY_READ, &k) != ERROR_SUCCESS) return;
    DWORD type = 0, size = sizeof(ULONGLONG);
    ULONGLONG v = 0;
    if (RegQueryValueExW(k, L"LastUsedTimeStart", NULL, &type, (LPBYTE)&v, &size) == ERROR_SUCCESS &&
        type == REG_QWORD)
        *start = v;
    size = sizeof(ULONGLONG);
    v = 0;
    if (RegQueryValueExW(k, L"LastUsedTimeStop", NULL, &type, (LPBYTE)&v, &size) == ERROR_SUCCESS &&
        type == REG_QWORD)
        *stop = v;
    RegCloseKey(k);
}

static int write_consent_value(const wchar_t *subkey, int allow)
{
    HKEY k;
    LONG r = RegCreateKeyExW(HKEY_CURRENT_USER, subkey, 0, NULL, 0, KEY_SET_VALUE, NULL, &k, NULL);
    if (r != ERROR_SUCCESS) return (int)r;
    const wchar_t *s = allow ? L"Allow" : L"Deny";
    r = RegSetValueExW(k, L"Value", 0, REG_SZ, (const BYTE *)s, (DWORD)((wcslen(s) + 1) * sizeof(wchar_t)));
    RegCloseKey(k);
    return (int)r;
}

static int clear_consent_value(const wchar_t *subkey)
{
    HKEY k;
    LONG r = RegOpenKeyExW(HKEY_CURRENT_USER, subkey, 0, KEY_SET_VALUE, &k);
    if (r == ERROR_FILE_NOT_FOUND) return 0;
    if (r != ERROR_SUCCESS) return (int)r;
    r = RegDeleteValueW(k, L"Value");
    RegCloseKey(k);
    if (r == ERROR_FILE_NOT_FOUND) return 0;
    return (int)r;
}

static void read_app_perms(AmApp *a)
{
    for (int cap = 0; cap < AM_CAP_COUNT; ++cap) {
        wchar_t sub[1024];
        a->perm[cap]       = -1;
        a->in_use[cap]     = 0;
        a->last_start[cap] = 0;
        a->last_stop[cap]  = 0;
        if (cap_key_path(cap, a->path, sub, 1024) != 0) continue;
        a->perm[cap] = read_consent_value(sub);

        {
            ULONGLONG st = 0, sp = 0;
            read_last_used(sub, &st, &sp);
            a->in_use[cap]     = (st != 0 && sp == 0) ? 1 : 0;
            a->last_start[cap] = (long long)st;
            a->last_stop[cap]  = (long long)sp;
        }
    }
}

static void collect_registry_apps(void)
{
    for (int cap = 0; cap < AM_CAP_COUNT; ++cap) {
        wchar_t sub[1024];
        if (cap_key_path(cap, NULL, sub, 1024) != 0) continue;
        HKEY k;
        if (RegOpenKeyExW(HKEY_CURRENT_USER, sub, 0, KEY_READ, &k) != ERROR_SUCCESS) continue;
        for (DWORD i = 0;; ++i) {
            wchar_t name[256];
            DWORD   nl = 256;
            LONG    r = RegEnumKeyExW(k, i, name, &nl, NULL, NULL, NULL, NULL);
            if (r == ERROR_NO_MORE_ITEMS) break;
            if (r != ERROR_SUCCESS) continue;
            wchar_t path[AM_PATH_MAX];
            decode_name(name, path, AM_PATH_MAX);
            if (ends_with_exe(path)) list_add(path);
        }
        RegCloseKey(k);
    }
}

static int is_managed(const wchar_t *path)
{
    HKEY k;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, MANAGED_KEY, 0, KEY_READ, &k) != ERROR_SUCCESS) return 0;
    LONG r = RegQueryValueExW(k, path, NULL, NULL, NULL, NULL);
    RegCloseKey(k);
    return r == ERROR_SUCCESS;
}

static void managed_set(const wchar_t *path, int on)
{
    if (on) {
        HKEY k;
        if (RegCreateKeyExW(HKEY_CURRENT_USER, MANAGED_KEY, 0, NULL, 0, KEY_SET_VALUE, NULL, &k, NULL) ==
            ERROR_SUCCESS) {
            DWORD one = 1;
            RegSetValueExW(k, path, 0, REG_DWORD, (const BYTE *)&one, sizeof(one));
            RegCloseKey(k);
        }
    } else {
        HKEY k;
        if (RegOpenKeyExW(HKEY_CURRENT_USER, MANAGED_KEY, 0, KEY_SET_VALUE, &k) == ERROR_SUCCESS) {
            RegDeleteValueW(k, path);
            RegCloseKey(k);
        }
    }
}

static void collect_managed_apps(void)
{
    HKEY k;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, MANAGED_KEY, 0, KEY_READ, &k) != ERROR_SUCCESS) return;
    for (DWORD i = 0;; ++i) {
        wchar_t name[AM_PATH_MAX];
        DWORD   nl = AM_PATH_MAX;
        LONG    r = RegEnumValueW(k, i, name, &nl, NULL, NULL, NULL, NULL);
        if (r == ERROR_NO_MORE_ITEMS) break;
        if (r != ERROR_SUCCESS) continue;
        list_add(name);
    }
    RegCloseKey(k);
}

static int process_path(DWORD pid, wchar_t *out, DWORD n)
{
    if (pid == 0) return 0;
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return 0;
    DWORD len = n;
    BOOL  ok  = QueryFullProcessImageNameW(h, 0, out, &len);
    CloseHandle(h);
    return ok ? 1 : 0;
}

static void collect_running_apps(void)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return;
    PROCESSENTRY32W pe;
    pe.dwSize = sizeof(pe);
    if (Process32FirstW(snap, &pe)) {
        do {
            wchar_t path[AM_PATH_MAX];
            if (process_path(pe.th32ProcessID, path, AM_PATH_MAX) && ends_with_exe(path)) list_add(path);
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
}

static BOOL com_enter(void)
{
    return SUCCEEDED(CoInitializeEx(NULL, COINIT_APARTMENTTHREADED));
}
static void com_leave(BOOL inited)
{
    if (inited) CoUninitialize();
}

typedef void (*AudioCb)(const wchar_t *path, ISimpleAudioVolume *vol, void *ctx);

static HRESULT audio_enum(AudioCb cb, void *ctx)
{
    IMMDeviceEnumerator *en = NULL;
    HRESULT hr = CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL, &IID_IMMDeviceEnumerator,
                                  (void **)&en);
    if (FAILED(hr)) return hr;

    IMMDeviceCollection *col = NULL;
    hr = IMMDeviceEnumerator_EnumAudioEndpoints(en, eRender, DEVICE_STATE_ACTIVE, &col);
    if (SUCCEEDED(hr)) {
        UINT ndev = 0;
        IMMDeviceCollection_GetCount(col, &ndev);
        for (UINT d = 0; d < ndev; ++d) {
            IMMDevice *dev = NULL;
            if (FAILED(IMMDeviceCollection_Item(col, d, &dev))) continue;

            IAudioSessionManager2 *mgr = NULL;
            if (SUCCEEDED(IMMDevice_Activate(dev, &IID_IAudioSessionManager2, CLSCTX_ALL, NULL, (void **)&mgr))) {
                IAudioSessionEnumerator *se = NULL;
                if (SUCCEEDED(IAudioSessionManager2_GetSessionEnumerator(mgr, &se))) {
                    int ns = 0;
                    IAudioSessionEnumerator_GetCount(se, &ns);
                    for (int s = 0; s < ns; ++s) {
                        IAudioSessionControl *ctl = NULL;
                        if (FAILED(IAudioSessionEnumerator_GetSession(se, s, &ctl))) continue;

                        IAudioSessionControl2 *c2  = NULL;
                        ISimpleAudioVolume    *vol = NULL;
                        if (SUCCEEDED(IAudioSessionControl_QueryInterface(ctl, &IID_IAudioSessionControl2,
                                                                          (void **)&c2)) &&
                            SUCCEEDED(IAudioSessionControl_QueryInterface(ctl, &IID_ISimpleAudioVolume,
                                                                          (void **)&vol))) {
                            if (IAudioSessionControl2_IsSystemSoundsSession(c2) != S_OK) {
                                DWORD   pid = 0;
                                wchar_t path[AM_PATH_MAX];
                                if (SUCCEEDED(IAudioSessionControl2_GetProcessId(c2, &pid)) &&
                                    process_path(pid, path, AM_PATH_MAX))
                                    cb(path, vol, ctx);
                            }
                        }
                        if (vol) ISimpleAudioVolume_Release(vol);
                        if (c2) IAudioSessionControl2_Release(c2);
                        IAudioSessionControl_Release(ctl);
                    }
                    IAudioSessionEnumerator_Release(se);
                }
                IAudioSessionManager2_Release(mgr);
            }
            IMMDevice_Release(dev);
        }
        IMMDeviceCollection_Release(col);
    }
    IMMDeviceEnumerator_Release(en);
    return hr;
}

static void cb_scan_audio(const wchar_t *path, ISimpleAudioVolume *vol, void *ctx)
{
    (void)ctx;
    BOOL muted = FALSE;
    if (FAILED(ISimpleAudioVolume_GetMute(vol, &muted))) return;
    AmApp *a = list_add(path);
    if (!a) return;
    if (a->audio == -2)
        a->audio = muted ? 0 : 1;
    else if (!muted)
        a->audio = 1;
}

typedef struct { const wchar_t *path; int total; int muted; } AudioQueryCtx;
static void cb_query_audio(const wchar_t *path, ISimpleAudioVolume *vol, void *ctx)
{
    AudioQueryCtx *q = (AudioQueryCtx *)ctx;
    if (_wcsicmp(path, q->path) != 0) return;
    BOOL muted = FALSE;
    if (FAILED(ISimpleAudioVolume_GetMute(vol, &muted))) return;
    q->total++;
    if (muted) q->muted++;
}

typedef struct { const wchar_t *path; BOOL mute; int changed; } AudioSetCtx;
static void cb_set_audio(const wchar_t *path, ISimpleAudioVolume *vol, void *ctx)
{
    AudioSetCtx *s = (AudioSetCtx *)ctx;
    if (_wcsicmp(path, s->path) != 0) return;
    if (SUCCEEDED(ISimpleAudioVolume_SetMute(vol, s->mute, NULL))) s->changed++;
}

typedef struct { INetFwPolicy2 *pol; INetFwRules *rules; } Fw;

static HRESULT fw_open(Fw *fw)
{
    fw->pol   = NULL;
    fw->rules = NULL;
    HRESULT hr = CoCreateInstance(&CLSID_NetFwPolicy2, NULL, CLSCTX_INPROC_SERVER, &IID_INetFwPolicy2,
                                  (void **)&fw->pol);
    if (FAILED(hr)) return hr;
    hr = INetFwPolicy2_get_Rules(fw->pol, &fw->rules);
    if (FAILED(hr)) {
        INetFwPolicy2_Release(fw->pol);
        fw->pol = NULL;
    }
    return hr;
}

static void fw_close(Fw *fw)
{
    if (fw->rules) INetFwRules_Release(fw->rules);
    if (fw->pol) INetFwPolicy2_Release(fw->pol);
    fw->rules = NULL;
    fw->pol   = NULL;
}

static void rule_name(const wchar_t *path, int inbound, wchar_t *out, size_t n)
{
    unsigned long long h = 1469598103934665603ULL;
    for (const wchar_t *p = path; *p; ++p) {
        wchar_t c = *p;
        if (c >= L'A' && c <= L'Z') c = (wchar_t)(c - L'A' + L'a');
        h ^= (unsigned long long)c;
        h *= 1099511628211ULL;
    }
    _snwprintf(out, n, L"AccessMonitor Block %ls %08lx%08lx", inbound ? L"In" : L"Out",
               (unsigned long)(h >> 32), (unsigned long)(h & 0xFFFFFFFFUL));
    out[n - 1] = 0;
}

static int fw_is_blocked(Fw *fw, const wchar_t *path)
{
    wchar_t name[128];
    rule_name(path, 0, name, 128);
    BSTR b = SysAllocString(name);
    if (!b) return 1;
    INetFwRule *rule = NULL;
    HRESULT hr = INetFwRules_Item(fw->rules, b, &rule);
    SysFreeString(b);
    if (hr == HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND)) return 0;
    if (SUCCEEDED(hr) && !rule) return 0;
    if (FAILED(hr)) return 1;
    VARIANT_BOOL en = VARIANT_FALSE;
    INetFwRule_get_Enabled(rule, &en);
    INetFwRule_Release(rule);
    return en == VARIANT_TRUE ? 1 : 0;
}

static void fw_remove(Fw *fw, const wchar_t *path, int inbound)
{
    wchar_t name[128];
    rule_name(path, inbound, name, 128);
    BSTR b = SysAllocString(name);
    if (!b) return;
    INetFwRules_Remove(fw->rules, b);
    SysFreeString(b);
}

static HRESULT fw_add_block(Fw *fw, const wchar_t *path, int inbound)
{
    fw_remove(fw, path, inbound);

    INetFwRule *r = NULL;
    HRESULT hr = CoCreateInstance(&CLSID_NetFwRule, NULL, CLSCTX_INPROC_SERVER, &IID_INetFwRule, (void **)&r);
    if (FAILED(hr)) return hr;

    wchar_t name[128];
    rule_name(path, inbound, name, 128);
    wchar_t desc[AM_PATH_MAX + 64];
    _snwprintf(desc, sizeof(desc) / sizeof(desc[0]), L"Blocked by Access Monitor: %ls", path);
    desc[sizeof(desc) / sizeof(desc[0]) - 1] = 0;

    BSTR bName = SysAllocString(name);
    BSTR bDesc = SysAllocString(desc);
    BSTR bApp  = SysAllocString(path);
    BSTR bGrp  = SysAllocString(L"Access Monitor");
    if (!bName || !bDesc || !bApp || !bGrp) {
        hr = E_OUTOFMEMORY;
    } else {
        hr = INetFwRule_put_Name(r, bName);
        if (SUCCEEDED(hr)) hr = INetFwRule_put_Description(r, bDesc);
        if (SUCCEEDED(hr)) hr = INetFwRule_put_ApplicationName(r, bApp);
        if (SUCCEEDED(hr)) hr = INetFwRule_put_Grouping(r, bGrp);
        if (SUCCEEDED(hr)) hr = INetFwRule_put_Action(r, NET_FW_ACTION_BLOCK);
        if (SUCCEEDED(hr)) hr = INetFwRule_put_Direction(r, inbound ? NET_FW_RULE_DIR_IN : NET_FW_RULE_DIR_OUT);
        if (SUCCEEDED(hr)) hr = INetFwRule_put_Profiles(r, NET_FW_PROFILE2_ALL);
        if (SUCCEEDED(hr)) hr = INetFwRule_put_Enabled(r, VARIANT_TRUE);
        if (SUCCEEDED(hr)) hr = INetFwRules_Add(fw->rules, r);
    }
    if (bName) SysFreeString(bName);
    if (bDesc) SysFreeString(bDesc);
    if (bApp)  SysFreeString(bApp);
    if (bGrp)  SysFreeString(bGrp);
    INetFwRule_Release(r);
    return hr;
}


AM_EXPORT int am_struct_size(void) { return (int)sizeof(AmApp); }

AM_EXPORT int am_is_admin(void)
{
    HANDLE tok = NULL;
    TOKEN_ELEVATION e;
    DWORD sz = 0;
    int   res = 0;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok)) {
        if (GetTokenInformation(tok, TokenElevation, &e, sizeof(e), &sz)) res = e.TokenIsElevated ? 1 : 0;
        CloseHandle(tok);
    }
    return res;
}

static void clipboard_scan(void)
{
    HWND owner = GetClipboardOwner();
    if (!owner) return;
    DWORD pid = 0;
    GetWindowThreadProcessId(owner, &pid);
    if (!pid) return;
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return;
    wchar_t path[AM_PATH_MAX];
    DWORD n = AM_PATH_MAX;
    if (QueryFullProcessImageNameW(h, 0, path, &n) && n > 0 && ends_with_exe(path)) {
        AmApp *a = list_add(path);
        if (a) a->clip = 1;
    }
    CloseHandle(h);
}

AM_EXPORT int am_scan(int include_running)
{
    BOOL com = com_enter();
    list_clear();

    collect_registry_apps();
    collect_managed_apps();
    if (include_running) collect_running_apps();
    audio_enum(cb_scan_audio, NULL);
    clipboard_scan();

    Fw   fw;
    int  fw_ok = 0;
    for (int i = 0; i < g_count; ++i) {
        read_app_perms(&g_apps[i]);
        if (is_managed(g_apps[i].path)) {
            if (!fw_ok) fw_ok = SUCCEEDED(fw_open(&fw)) ? 1 : -1;
            if (fw_ok == 1) g_apps[i].net = fw_is_blocked(&fw, g_apps[i].path);
        }
    }
    if (fw_ok == 1) fw_close(&fw);

    com_leave(com);
    return g_count;
}

AM_EXPORT int am_count(void) { return g_count; }

AM_EXPORT int am_get(int index, AmApp *out)
{
    if (!out || index < 0 || index >= g_count) return AM_ERR_BAD_ARG;
    *out = g_apps[index];
    return 0;
}

AM_EXPORT int am_query(const wchar_t *path, AmApp *out)
{
    if (!path || !*path || !out) return AM_ERR_BAD_ARG;
    if (wcslen(path) >= AM_PATH_MAX) return AM_ERR_PATH_LONG;

    BOOL com = com_enter();
    memset(out, 0, sizeof(*out));
    wcsncpy(out->path, path, AM_PATH_MAX - 1);
    out->audio = -2;
    read_app_perms(out);

    AudioQueryCtx q = { path, 0, 0 };
    audio_enum(cb_query_audio, &q);
    if (q.total > 0) out->audio = (q.muted == q.total) ? 0 : 1;

    if (is_managed(path)) {
        Fw fw;
        if (SUCCEEDED(fw_open(&fw))) {
            out->net = fw_is_blocked(&fw, path);
            fw_close(&fw);
        }
    }
    com_leave(com);
    return 0;
}

AM_EXPORT int am_get_global(int cap)
{
    wchar_t sub[1024];
    if (cap_key_path(cap, NULL, sub, 1024) != 0) return -1;
    return read_consent_value(sub);
}

AM_EXPORT int am_set_global(int cap, int allow)
{
    wchar_t sub[1024];
    int e = cap_key_path(cap, NULL, sub, 1024);
    if (e) return e;
    return write_consent_value(sub, allow);
}

AM_EXPORT int am_clear_global(int cap)
{
    wchar_t sub[1024];
    int e = cap_key_path(cap, NULL, sub, 1024);
    if (e) return e;
    return clear_consent_value(sub);
}

AM_EXPORT int am_set_permission(const wchar_t *path, int cap, int allow)
{
    if (!path || !*path) return AM_ERR_BAD_ARG;
    wchar_t sub[1024];
    int e = cap_key_path(cap, path, sub, 1024);
    if (e) return e;
    return write_consent_value(sub, allow);
}

AM_EXPORT int am_clear_permission(const wchar_t *path, int cap)
{
    if (!path || !*path) return AM_ERR_BAD_ARG;
    wchar_t sub[1024];
    int e = cap_key_path(cap, path, sub, 1024);
    if (e) return e;
    return clear_consent_value(sub);
}

AM_EXPORT int am_set_audio_mute(const wchar_t *path, int mute)
{
    if (!path || !*path) return AM_ERR_BAD_ARG;
    BOOL com = com_enter();
    AudioSetCtx s = { path, mute ? TRUE : FALSE, 0 };
    HRESULT hr = audio_enum(cb_set_audio, &s);
    com_leave(com);
    if (FAILED(hr)) return (int)hr;
    return s.changed > 0 ? 0 : AM_ERR_NO_SESSION;
}

AM_EXPORT int am_set_network_block(const wchar_t *path, int block)
{
    if (!path || !*path) return AM_ERR_BAD_ARG;
    if (wcslen(path) >= AM_PATH_MAX) return AM_ERR_PATH_LONG;

    BOOL    com = com_enter();
    Fw      fw;
    HRESULT hr = fw_open(&fw);
    if (SUCCEEDED(hr)) {
        if (block) {
            hr = fw_add_block(&fw, path, 0);
            if (SUCCEEDED(hr)) {
                hr = fw_add_block(&fw, path, 1);
                if (FAILED(hr)) fw_remove(&fw, path, 0);
            }
            if (SUCCEEDED(hr)) managed_set(path, 1);
        } else {
            fw_remove(&fw, path, 0);
            fw_remove(&fw, path, 1);
            managed_set(path, 0);
            hr = S_OK;
        }
        fw_close(&fw);
    }
    com_leave(com);
    return (int)hr;
}


static const wchar_t CLIPBOARD_KEY[] = L"Software\\Microsoft\\Clipboard";

AM_EXPORT int am_get_clipboard_history(void)
{
    HKEY k;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, CLIPBOARD_KEY, 0, KEY_READ, &k) != ERROR_SUCCESS)
        return 1;
    DWORD type = 0, v = 0, size = sizeof(v);
    int res = 1;
    if (RegQueryValueExW(k, L"EnableClipboardHistory", NULL, &type, (LPBYTE)&v, &size) == ERROR_SUCCESS &&
        type == REG_DWORD)
        res = v ? 1 : 0;
    RegCloseKey(k);
    return res;
}

AM_EXPORT int am_set_clipboard_history(int on)
{
    HKEY k;
    LONG r = RegCreateKeyExW(HKEY_CURRENT_USER, CLIPBOARD_KEY, 0, NULL, 0, KEY_SET_VALUE, NULL, &k, NULL);
    if (r != ERROR_SUCCESS) return (int)r;
    DWORD v = on ? 1 : 0;
    r = RegSetValueExW(k, L"EnableClipboardHistory", 0, REG_DWORD, (const BYTE *)&v, sizeof(v));
    RegCloseKey(k);
    return (int)r;
}

static HRESULT endpoint_volume(IAudioEndpointVolume **out)
{
    *out = NULL;
    IMMDeviceEnumerator *en = NULL;
    HRESULT hr = CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL, &IID_IMMDeviceEnumerator,
                                  (void **)&en);
    if (FAILED(hr)) return hr;
    IMMDevice *dev = NULL;
    hr = IMMDeviceEnumerator_GetDefaultAudioEndpoint(en, eRender, eMultimedia, &dev);
    if (SUCCEEDED(hr)) {
        hr = IMMDevice_Activate(dev, &IID_IAudioEndpointVolume, CLSCTX_ALL, NULL, (void **)out);
        IMMDevice_Release(dev);
    }
    IMMDeviceEnumerator_Release(en);
    return hr;
}

AM_EXPORT int am_get_audio_master(void)
{
    BOOL com = com_enter();
    int res = -1;
    IAudioEndpointVolume *vol = NULL;
    if (SUCCEEDED(endpoint_volume(&vol)) && vol) {
        BOOL muted = FALSE;
        if (SUCCEEDED(IAudioEndpointVolume_GetMute(vol, &muted))) res = muted ? 0 : 1;
        IAudioEndpointVolume_Release(vol);
    }
    com_leave(com);
    return res;
}

AM_EXPORT int am_set_audio_master(int on)
{
    BOOL com = com_enter();
    HRESULT hr = E_FAIL;
    IAudioEndpointVolume *vol = NULL;
    if (SUCCEEDED(endpoint_volume(&vol)) && vol) {
        hr = IAudioEndpointVolume_SetMute(vol, on ? FALSE : TRUE, NULL);
        IAudioEndpointVolume_Release(vol);
    }
    com_leave(com);
    return (int)hr;
}

AM_EXPORT int am_get_net_master(void)
{
    BOOL com = com_enter();
    int res = -1;
    Fw fw;
    if (SUCCEEDED(fw_open(&fw))) {
        long profiles = 0;
        if (SUCCEEDED(INetFwPolicy2_get_CurrentProfileTypes(fw.pol, &profiles))) {
            res = 1;
            for (int bit = 0; bit < 3; ++bit) {
                long one = 1L << bit;
                if (!(profiles & one)) continue;
                NET_FW_ACTION act = NET_FW_ACTION_ALLOW;
                if (SUCCEEDED(INetFwPolicy2_get_DefaultOutboundAction(fw.pol, (NET_FW_PROFILE_TYPE2)one, &act))
                    && act == NET_FW_ACTION_BLOCK)
                    res = 0;
            }
        }
        fw_close(&fw);
    }
    com_leave(com);
    return res;
}

AM_EXPORT int am_set_net_master(int on)
{
    BOOL com = com_enter();
    HRESULT hr = E_FAIL;
    Fw fw;
    if (SUCCEEDED(fw_open(&fw))) {
        long profiles = 0;
        hr = INetFwPolicy2_get_CurrentProfileTypes(fw.pol, &profiles);
        if (SUCCEEDED(hr)) {
            NET_FW_ACTION act = on ? NET_FW_ACTION_ALLOW : NET_FW_ACTION_BLOCK;
            for (int bit = 0; bit < 3; ++bit) {
                long one = 1L << bit;
                if (!(profiles & one)) continue;
                HRESULT h2 = INetFwPolicy2_put_DefaultOutboundAction(fw.pol, (NET_FW_PROFILE_TYPE2)one, act);
                if (FAILED(h2)) hr = h2;
            }
        }
        fw_close(&fw);
    }
    com_leave(com);
    return (int)hr;
}

AM_EXPORT int am_error_text(int code, wchar_t *buf, int n)
{
    if (!buf || n <= 0) return 0;
    buf[0] = 0;
    const wchar_t *msg = NULL;
    switch ((unsigned)code) {
    case AM_ERR_BAD_ARG:    msg = L"Invalid argument"; break;
    case AM_ERR_NO_SESSION: msg = L"This app has no audio session right now (it is not playing sound)"; break;
    case AM_ERR_PATH_LONG:  msg = L"The path is too long"; break;
    default: break;
    }
    if (msg) {
        wcsncpy(buf, msg, (size_t)n - 1);
        buf[n - 1] = 0;
        return (int)wcslen(buf);
    }

    DWORD c = (DWORD)code;
    if (code < 0 && HRESULT_FACILITY((HRESULT)code) == FACILITY_WIN32) c = HRESULT_CODE((HRESULT)code);
    DWORD len = FormatMessageW(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, NULL, c, 0, buf,
                               (DWORD)n, NULL);
    while (len > 0 && (buf[len - 1] == L'\r' || buf[len - 1] == L'\n' || buf[len - 1] == L' ')) buf[--len] = 0;
    if (len == 0) {
        _snwprintf(buf, (size_t)n, L"Error 0x%08X", (unsigned)code);
        buf[n - 1] = 0;
        len = (DWORD)wcslen(buf);
    }
    return (int)len;
}
