import { atom } from 'recoil';
import Cookies from 'js-cookie';
import { atomWithLocalStorage } from './utils';

const readStoredLang = () => {
  if (typeof localStorage === 'undefined') {
    return undefined;
  }

  const storedLang = localStorage.getItem('lang');
  if (!storedLang) {
    return undefined;
  }

  try {
    const parsedLang = JSON.parse(storedLang);
    return typeof parsedLang === 'string' ? parsedLang : storedLang;
  } catch {
    return storedLang;
  }
};

const defaultLang = () => {
  return Cookies.get('lang') || readStoredLang() || 'zh-Hans';
};

const lang = atomWithLocalStorage('lang', defaultLang());
const languageLoading = atom<boolean>({
  key: 'languageLoading',
  default: false,
});

export default { lang, languageLoading };
