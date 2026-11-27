function PersonalWordFallback({ path, version }) {
    const t = useLocalize();
    const { token, user } = useAuthContext();
    const preview = useQuery({
        queryKey: ['personalWordPreview', user?.id, path, version],
        queryFn: async () => {
            const response = await personalFileRequest(token, version ? 'revisions' : 'file', 'GET', path, undefined, { version, preview: 'word' });
            return response.json();
        },
        enabled: !!token,
        retry: false,
        cacheTime: 0,
    });
    return Q.createElement("div", { className: "flex h-full flex-col" },
        preview.isError && Q.createElement("p", { role: "alert" }, t('com_ui_preview_unavailable')),
        preview.isLoading && Q.createElement("p", { role: "status" }, t('com_ui_loading')),
        preview.data && (Q.createElement("iframe", { title: t('com_office_word_preview'), sandbox: "allow-scripts", referrerPolicy: "no-referrer", className: "min-h-0 w-full flex-1 border-0", srcDoc: preview.data.html })));
}
function SelectedWordPreview({ path, version, onOpen }) {
    const { token } = useAuthContext();
    const name = path.split('/').pop() ?? path;
    return Q.createElement(WordPages, { filename: version ? name + ' · v' + version : name, onOpen: onOpen, fallback: Q.createElement(PersonalWordFallback, { path: path, version: version }), load: async () => (await personalFileRequest(token, version ? 'revisions' : 'file', 'GET', path, undefined, { version })).blob() });
}
