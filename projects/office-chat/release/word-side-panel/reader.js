const { WordPages, useWordSidePanel }=(()=>{const {useEffect,useRef,useState}=Q,atom=H,useAtom=rt,useSetAtom=U,useRecoilValue=W,useResetRecoilState=Wt,activeSubagentPanel=ym,apiBaseUrl=Qo,Button=q,useAuthContext=gg,useGetStartupConfig=Mv,store=Y; const labels={"en":{"com_ui_preview_word":"Preview Word","com_word_fit_width":"Fit to width","com_ui_zoom_in":"Zoom in","com_ui_zoom_out":"Zoom out"},"zh-Hans":{"com_ui_preview_word":"预览 Word","com_word_fit_width":"适应面板宽度","com_ui_zoom_in":"放大","com_ui_zoom_out":"缩小"}};const useLocalize=()=>{const t=X(),d=labels[/保存|儲存/.test(t("com_ui_save"))?"zh-Hans":"en"];return k=>d[k]??t(k)};
const selectedWord = atom(null);
function WordPages({ filename, load, onOpen, fallback }) {
    const { data } = useGetStartupConfig();
    const t = useLocalize();
    const conversationId = useRecoilValue(store.conversationIdByIndex(0)) ?? null;
    const resetArtifact = useResetRecoilState(store.currentArtifactId);
    const clearSubagent = useSetAtom(activeSubagentPanel);
    const select = useSetAtom(selectedWord);
    if (!data?.wordPages && !fallback)
        return null;
    return (Q.createElement(Button, { variant: "outline", onClick: (event) => {
            resetArtifact();
            clearSubagent(null);
            select({ filename, load, fallback: data?.wordPages ? undefined : fallback,
                conversationId, opener: event.currentTarget });
            onOpen?.();
        } }, t('com_ui_preview_word')));
}
function useWordSidePanel(conversationId, otherPanelOpen) {
    const [file, select] = useAtom(selectedWord);
    const visible = file != null && file.conversationId === conversationId && !otherPanelOpen;
    useEffect(() => {
        if (file && !visible)
            select(null);
    }, [file, visible, select]);
    useEffect(() => () => { select(null); }, [select]);
    if (!visible)
        return null;
    return Q.createElement(WordReader, { file: file, close: () => {
            select(null);
            if (file.opener.isConnected)
                file.opener.focus();
        } });
}
function WordReader({ file, close }) {
    const { token } = useAuthContext();
    const t = useLocalize();
    const [result, setResult] = useState();
    const [attempt, retry] = useState(0);
    const [zoom, setZoom] = useState(100);
    const title = useRef(null);
    const viewport = useRef(null);
    useEffect(() => {
        title.current?.focus({ preventScroll: true });
        setZoom(100);
        viewport.current?.scrollTo(0, 0);
    }, [file]);
    useEffect(() => {
        if (file.fallback)
            return;
        const request = new AbortController();
        setResult(undefined);
        async function render() {
            try {
                const blob = await file.load();
                if (request.signal.aborted)
                    return;
                if (!token || blob.size > 16 * 1024 * 1024)
                    throw Error('invalid_file');
                const hash = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
                const digest = Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('');
                const response = await fetch(apiBaseUrl() + '/api/files/word-pages', {
                    method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/octet-stream' },
                    body: blob, signal: request.signal, redirect: 'error',
                });
                if (!response.ok)
                    throw Error('render_failed');
                const data = await response.json();
                if (data.sha256 !== digest || !Array.isArray(data.pages) || !data.pages.length ||
                    data.pages.length > 40 || !data.pages.every((p) => typeof p === 'string' && p.startsWith('/9j/'))) {
                    throw Error('invalid_pages');
                }
                if (!request.signal.aborted)
                    setResult({ file, pages: data.pages });
            }
            catch {
                if (!request.signal.aborted)
                    setResult({ file, failed: true });
            }
        }
        void render();
        return () => request.abort();
    }, [file, token, attempt]);
    const pages = result?.file === file ? result.pages : undefined;
    return (Q.createElement("section", { "aria-label": file.filename, "data-testid": "word-side-preview", className: "bg-surface-primary text-text-primary flex h-full min-w-0 flex-col", onKeyDown: event => { if (event.key === 'Escape') {
            event.stopPropagation();
            close();
        } } },
        Q.createElement("header", { className: "border-border-light flex items-center gap-2 border-b px-3 py-2" },
            Q.createElement("h2", { ref: title, tabIndex: -1, title: file.filename, className: "min-w-0 flex-1 truncate text-sm font-semibold" }, file.filename),
            pages && Q.createElement(Q.Fragment, null,
                Q.createElement(Button, { size: "icon", variant: "ghost", "aria-label": t('com_ui_zoom_out'), disabled: zoom <= 75, onClick: () => setZoom(z => Math.max(75, z - 25)) }, "\u2212"),
                Q.createElement("button", { type: "button", title: t('com_word_fit_width'), onClick: () => setZoom(100), className: "text-xs" },
                    zoom,
                    "%"),
                Q.createElement(Button, { size: "icon", variant: "ghost", "aria-label": t('com_ui_zoom_in'), disabled: zoom >= 200, onClick: () => setZoom(z => Math.min(200, z + 25)) }, "+")),
            Q.createElement(Button, { size: "icon", variant: "ghost", "aria-label": t('com_ui_close'), onClick: close }, "\u00D7")),
        Q.createElement("div", { ref: viewport, className: "bg-surface-secondary min-h-0 flex-1 overflow-auto p-3" }, file.fallback ?? (pages ? (Q.createElement("div", { className: "mx-auto", style: { width: zoom + '%' } }, pages.map((page, i) => (Q.createElement("figure", { key: i, className: "mb-3" },
            Q.createElement("img", { src: 'data:image/jpeg;base64,' + page, alt: file.filename + ' · ' + (i + 1) + ' / ' + pages.length, loading: i === 0 ? 'eager' : 'lazy', className: "block h-auto w-full bg-white shadow" }),
            Q.createElement("figcaption", { className: "text-text-secondary mt-1 text-center text-xs" },
                i + 1,
                " / ",
                pages.length)))))) : result?.file === file && result.failed ? (Q.createElement("div", { role: "alert" },
            t('com_ui_preview_unavailable'),
            " ",
            Q.createElement(Button, { variant: "outline", onClick: () => retry(x => x + 1) }, t('com_ui_retry')))) : Q.createElement("p", { role: "status" }, t('com_ui_loading'))))));
}

return {WordPages,useWordSidePanel};})();
