"""Automação Selenium do portal Sitrax / Recipe Tracker."""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from app.config import settings
from app.bot.report import Position, positions_from_rows
from app.bot import debug_session

logger = logging.getLogger(__name__)
DEBUG_DIR = Path(__file__).resolve().parents[2] / "debug"
DEBUG_DIR.mkdir(exist_ok=True)


class SitraxBot:
    """
    Fluxo:
      1. Login (Cliente / Usuário / Senha)
      2. Menu → Históricos → Posições
      3. Filtros → Veículo
      4. Lupa da coluna Placa
      5. Seleciona veículo → Selecionar
      6. Filtrar e ler tabela
    """

    def __init__(
        self,
        cliente: Optional[str] = None,
        usuario: Optional[str] = None,
        senha: Optional[str] = None,
        login_url: Optional[str] = None,
        headless: Optional[bool] = None,
        download_dir: Optional[Path | str] = None,
        quiet: bool = False,
        low_memory: bool = False,
    ):
        self.cliente = cliente or settings.sitrax_cliente
        self.usuario = usuario or settings.sitrax_usuario
        self.senha = senha or settings.sitrax_senha
        self.login_url = login_url or settings.sitrax_url
        self.headless = settings.sitrax_headless if headless is None else headless
        # Downloads do Sitrax só no servidor (pasta temp) — nunca no celular do usuário
        self.download_dir = Path(download_dir) if download_dir else None
        # frota: menos screenshots / menos RAM
        self.quiet = quiet
        self.low_memory = low_memory or quiet
        self.driver: Optional[webdriver.Chrome] = None
        self.wait: Optional[WebDriverWait] = None
        self._chrome_profile: Optional[Path] = None

    def __enter__(self) -> "SitraxBot":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    async def __aenter__(self) -> "SitraxBot":
        self.start()
        return self

    async def __aexit__(self, *args) -> None:
        self.close()

    def start(self) -> None:
        if not self.cliente or not self.usuario or not self.senha:
            raise ValueError(
                "Configure SITRAX_CLIENTE, SITRAX_USUARIO e SITRAX_SENHA no .env"
            )
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        else:
            opts.add_argument("--start-maximized")
        # Flags críticas Docker/Railway (tab crashed = quase sempre RAM/shm)
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--disable-sync")
        opts.add_argument("--disable-translate")
        opts.add_argument("--metrics-recording-only")
        opts.add_argument("--mute-audio")
        opts.add_argument("--no-first-run")
        opts.add_argument("--safebrowsing-disable-auto-update")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--disable-hang-monitor")
        opts.add_argument("--disable-component-update")
        opts.add_argument("--disable-domain-reliability")
        opts.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees,AudioServiceOutOfProcess")
        opts.add_argument("--disable-ipc-flooding-protection")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument("--disable-backgrounding-occluded-windows")
        opts.add_argument("--disable-client-side-phishing-detection")
        opts.add_argument("--memory-pressure-off")
        # 1 renderer — frota longa no Railway
        opts.add_argument("--renderer-process-limit=1")
        opts.add_argument("--js-flags=--max-old-space-size=256")
        # viewport desktop menor = menos RAM (ainda não é mobile)
        if self.low_memory:
            opts.add_argument("--window-size=1366,768")
            opts.add_argument("--force-device-scale-factor=1")
        else:
            opts.add_argument("--window-size=1600,900")
            opts.add_argument("--force-device-scale-factor=1")
        opts.add_argument("--window-position=0,0")
        opts.add_argument("--lang=pt-BR")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        # perfil ÚNICO por sessão (evita lock/corrupção e vazamento de cache)
        import tempfile as _tmpmod
        import uuid as _uuid

        _chrome_tmp = Path(_tmpmod.gettempdir()) / "sitrax-chrome" / _uuid.uuid4().hex[:12]
        _chrome_tmp.mkdir(parents=True, exist_ok=True)
        self._chrome_profile = _chrome_tmp
        opts.add_argument(f"--user-data-dir={_chrome_tmp / 'user-data'}")
        opts.add_argument(f"--disk-cache-dir={_chrome_tmp / 'cache'}")
        opts.add_argument("--disk-cache-size=1")
        opts.add_argument("--media-cache-size=1")
        # evita popups "Salvar senha?" / "Usar chave de acesso?" do Chrome
        opts.add_experimental_option(
            "excludeSwitches", ["enable-automation", "enable-logging"]
        )
        prefs = {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "profile.password_manager_leak_detection": False,
            "autofill.profile_enabled": False,
            "autofill.credit_card_enabled": False,
        }
        # Download automático só na pasta TEMP do servidor
        if self.download_dir:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            prefs.update(
                {
                    "download.default_directory": str(self.download_dir.resolve()),
                    "download.prompt_for_download": False,
                    "download.directory_upgrade": True,
                    "plugins.always_open_pdf_externally": True,
                    "safebrowsing.enabled": True,
                }
            )
        opts.add_experimental_option("prefs", prefs)

        # Preferir Chrome do sistema (Docker); fallback webdriver-manager
        try:
            self.driver = webdriver.Chrome(options=opts)
        except Exception as e:
            logger.warning("Chrome default falhou (%s); tentando ChromeDriverManager", e)
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=opts)
        self.wait = WebDriverWait(self.driver, 45)
        self.driver.set_page_load_timeout(90)

        # Força tela DESKTOP (não minimizada / não mobile)
        try:
            if not self.headless:
                try:
                    self.driver.maximize_window()
                except Exception:
                    self.driver.set_window_rect(0, 0, 1920, 1080)
            else:
                w, h = (1366, 768) if self.low_memory else (1600, 900)
                self.driver.set_window_size(w, h)
        except Exception as e:
            logger.warning("Ajuste de janela: %s", e)

        # CDP: viewport desktop fixo
        try:
            w, h = (1366, 768) if self.low_memory else (1600, 900)
            self.driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": w,
                    "height": h,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                    "screenWidth": w,
                    "screenHeight": h,
                },
            )
            self.driver.execute_cdp_cmd(
                "Emulation.setTouchEmulationEnabled",
                {"enabled": False},
            )
        except Exception as e:
            logger.warning("CDP desktop viewport: %s", e)

        self._ensure_download_behavior()

        self._trace(
            "chrome_pronto",
            f"Chrome desktop low_mem={self.low_memory} headless={self.headless}",
            shot=not self.quiet,
        )

    def _ensure_download_behavior(self) -> None:
        """Garante que PDF do Sitrax caia na pasta temp (headless Docker/Railway)."""
        if not self.driver or not self.download_dir:
            return
        path = str(self.download_dir.resolve())
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # Browser (Chrome moderno) + Page (fallback)
        for cmd, params in (
            (
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": path,
                    "eventsEnabled": True,
                },
            ),
            (
                "Page.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": path},
            ),
        ):
            try:
                self.driver.execute_cdp_cmd(cmd, params)
            except Exception as e:
                logger.debug("CDP %s: %s", cmd, e)
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

    def close(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver = None
        self.wait = None
        # apaga perfil temp (libera disco/RAM no Railway)
        if self._chrome_profile and self._chrome_profile.exists():
            try:
                import shutil

                shutil.rmtree(self._chrome_profile, ignore_errors=True)
            except Exception:
                pass
        self._chrome_profile = None

    def alive(self) -> bool:
        """False se a aba/sessão do Chrome morreu (tab crashed)."""
        if not self.driver:
            return False
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    def _d(self) -> webdriver.Chrome:
        if not self.driver:
            raise RuntimeError("Bot não iniciado")
        return self.driver

    def _w(self) -> WebDriverWait:
        if not self.wait:
            raise RuntimeError("Bot não iniciado")
        return self.wait

    def _sleep(self, sec: float = 0.8) -> None:
        time.sleep(sec)

    def _click(self, element) -> None:
        d = self._d()
        try:
            d.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", element
            )
        except Exception:
            pass
        try:
            element.click()
        except (ElementClickInterceptedException, StaleElementReferenceException):
            try:
                d.execute_script("arguments[0].click();", element)
            except Exception:
                ActionChains(d).move_to_element(element).click().perform()

    def _save_debug(self, label: str, message: str = "", ok: bool = True) -> Path:
        """Salva screenshot + HTML e registra no painel de calibração."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)[:40]
        png = DEBUG_DIR / f"{ts}_{safe}.png"
        html = DEBUG_DIR / f"{ts}_{safe}.html"
        try:
            self._d().save_screenshot(str(png))
            html.write_text(self._d().page_source, encoding="utf-8", errors="replace")
            logger.info("Debug salvo: %s | %s | URL=%s", png, html, self._d().current_url)
        except Exception as e:
            logger.warning("Falha ao salvar debug: %s", e)
        # painel /debug (memória)
        try:
            debug_session.step(
                label,
                message or label,
                driver=self._d() if self.driver else None,
                ok=ok,
                screenshot=True,
                html=False,
            )
        except Exception:
            pass
        return png

    def _trace(self, name: str, message: str = "", ok: bool = True, shot: bool = True) -> None:
        """Passo leve para o painel de calibração."""
        # frota: screenshots consomem RAM e derrubam o Chrome
        if self.quiet:
            shot = False
        try:
            debug_session.step(
                name,
                message,
                driver=self._d() if (self.driver and shot) else None,
                ok=ok,
                screenshot=shot,
            )
        except Exception:
            pass

    def _find_first(self, selectors: list[tuple[str, str]], timeout: float = 15):
        end = time.time() + timeout
        last_err = None
        while time.time() < end:
            for by, value in selectors:
                try:
                    els = self._d().find_elements(by, value)
                    for el in els:
                        try:
                            if el.is_displayed():
                                return el
                        except StaleElementReferenceException:
                            continue
                except Exception as e:
                    last_err = e
            time.sleep(0.3)
        raise TimeoutException(f"Não encontrou: {selectors} ({last_err})")

    def _click_by_text(self, texts: list[str], timeout: float = 12) -> bool:
        """Clica no primeiro elemento visível cujo texto contenha alguma das strings."""
        end = time.time() + timeout
        while time.time() < end:
            for text in texts:
                xpaths = [
                    f"//a[contains(normalize-space(.), '{text}')]",
                    f"//button[contains(normalize-space(.), '{text}')]",
                    f"//span[contains(normalize-space(.), '{text}')]",
                    f"//div[contains(normalize-space(.), '{text}')]",
                    f"//li[contains(normalize-space(.), '{text}')]",
                    f"//*[self::a or self::button or self::span or self::div or self::li]"
                    f"[contains(normalize-space(.), '{text}')]",
                ]
                for xp in xpaths:
                    try:
                        els = self._d().find_elements(By.XPATH, xp)
                    except Exception:
                        continue
                    for el in els:
                        try:
                            if not el.is_displayed():
                                continue
                            # evita clicar em blocos gigantes do layout
                            size = el.size
                            if size.get("height", 0) > 120 and el.tag_name.lower() in (
                                "div",
                                "section",
                                "main",
                            ):
                                continue
                            self._click(el)
                            return True
                        except StaleElementReferenceException:
                            continue
                        except Exception:
                            continue
            time.sleep(0.4)
        return False

    def login(self) -> None:
        d = self._d()
        logger.info("Abrindo login: %s", self.login_url)
        self._trace("login_abrir", f"Abrindo {self.login_url}")
        d.get(self.login_url)
        self._sleep(1.5)
        self._trace("login_form", "Formulário de login carregado")

        # 3 campos: Cliente, Usuário, Senha
        inputs = d.find_elements(By.CSS_SELECTOR, "input:not([type='hidden'])")
        visible = [i for i in inputs if i.is_displayed()]
        text_fields = []
        password = None
        for el in visible:
            t = (el.get_attribute("type") or "text").lower()
            if t == "password":
                password = el
            elif t in ("text", "email", "tel", ""):
                text_fields.append(el)

        if len(text_fields) >= 2 and password:
            text_fields[0].clear()
            text_fields[0].send_keys(self.cliente)
            text_fields[1].clear()
            text_fields[1].send_keys(self.usuario)
            password.clear()
            password.send_keys(self.senha)
        else:
            # fallback por name/id
            for name in ("cliente", "Cliente", "client"):
                try:
                    el = d.find_element(By.CSS_SELECTOR, f"input[name*='{name}' i], input[id*='{name}' i]")
                    el.clear()
                    el.send_keys(self.cliente)
                    break
                except NoSuchElementException:
                    continue
            for name in ("usuario", "user", "login"):
                try:
                    el = d.find_element(By.CSS_SELECTOR, f"input[name*='{name}' i], input[id*='{name}' i]")
                    if (el.get_attribute("type") or "").lower() != "password":
                        el.clear()
                        el.send_keys(self.usuario)
                        break
                except NoSuchElementException:
                    continue
            pwd = d.find_element(By.CSS_SELECTOR, "input[type='password']")
            pwd.clear()
            pwd.send_keys(self.senha)

        # Botão Entrar
        try:
            btn = self._find_first(
                [
                    (By.XPATH, "//button[contains(translate(., 'ENTRAR', 'entrar'), 'entrar')]"),
                    (By.CSS_SELECTOR, "button[type='submit']"),
                    (By.CSS_SELECTOR, "input[type='submit']"),
                ],
                timeout=10,
            )
            self._click(btn)
        except TimeoutException:
            password.send_keys("\n") if password else None

        self._sleep(2)
        try:
            self._w().until(
                lambda drv: "login" not in drv.current_url.lower()
                or "welcome" in drv.current_url.lower()
                or "secure" in drv.current_url.lower()
            )
        except TimeoutException:
            self._save_debug("login_falhou", "Login não concluiu", ok=False)
            raise TimeoutException(
                "Login não concluiu. Confira cliente/usuário/senha no .env"
            )
        # dashboard demora a carregar menus
        self._sleep(3)
        logger.info("Login ok: %s", d.current_url)
        self._save_debug("apos_login", f"Login OK — {d.current_url}")

    def _js_click_id(self, element_id: str) -> bool:
        """Clica por id (funciona mesmo se o item estiver no menu lateral off-screen)."""
        d = self._d()
        try:
            el = d.find_element(By.ID, element_id)
        except NoSuchElementException:
            logger.warning("ID não encontrado: %s", element_id)
            return False
        try:
            d.execute_script("arguments[0].click();", el)
            return True
        except Exception as e:
            logger.warning("JS click falhou em %s: %s", element_id, e)
            try:
                self._click(el)
                return True
            except Exception:
                return False

    def _js_call(self, code: str) -> None:
        self._d().execute_script(code)

    def _wait_loader_gone(self, timeout: float = 30) -> None:
        """Espera o loader JSF (swShowLoader) sumir."""
        d = self._d()
        end = time.time() + timeout
        self._sleep(0.5)
        while time.time() < end:
            try:
                loaders = d.find_elements(
                    By.CSS_SELECTOR,
                    ".swLoader, .ui-blockui, .blockUI, .rf-loading, "
                    "[id*='loader' i], [class*='loader' i], [class*='Loading']",
                )
                visible = False
                for el in loaders:
                    try:
                        if el.is_displayed():
                            visible = True
                            break
                    except StaleElementReferenceException:
                        continue
                if not visible:
                    # pequena folga pós-AJAX
                    self._sleep(0.8)
                    return
            except Exception:
                return
            time.sleep(0.3)

    def _open_side_menu(self) -> None:
        """Abre o menu ☰ lateral (swOpenSidebarMenu do Sitrax)."""
        d = self._d()
        # função nativa do sistema
        try:
            d.execute_script(
                "if (typeof swOpenSidebarMenu === 'function') { swOpenSidebarMenu(); }"
            )
            self._sleep(0.8)
        except Exception:
            pass

        # fallback: clicar no ícone
        for sel in [
            (By.CSS_SELECTOR, "a.swTopBarIconCloseLight"),
            (By.CSS_SELECTOR, "a[onclick*='swOpenSidebarMenu']"),
            (By.XPATH, "//a[contains(@onclick,'swOpenSidebarMenu')]"),
            (By.CSS_SELECTOR, "#sidebar-menu"),
        ]:
            try:
                el = d.find_element(*sel)
                if "sidebar-menu" in (el.get_attribute("id") or ""):
                    # só garante visível via JS
                    d.execute_script(
                        "arguments[0].classList.remove('-translate-x-full');"
                        "arguments[0].classList.add('translate-x-0');",
                        el,
                    )
                    self._sleep(0.4)
                    logger.info("Sidebar forçada visível")
                    return
                if el.is_displayed():
                    self._click(el)
                    self._sleep(0.6)
                    logger.info("Menu lateral aberto via %s", sel)
                    return
            except Exception:
                continue

        # força menu visível
        try:
            menu = d.find_element(By.ID, "sidebar-menu")
            d.execute_script(
                "arguments[0].classList.remove('-translate-x-full');"
                "arguments[0].style.transform='translateX(0)';"
                "arguments[0].style.display='block';",
                menu,
            )
            self._sleep(0.4)
        except Exception:
            logger.warning("Não abriu sidebar — tentando clicar nos itens mesmo assim")

    def _norm(self, s: str) -> str:
        """Remove acentos e lowercase para comparação."""
        import unicodedata

        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        return s.lower()

    def _page_blob(self) -> str:
        """Texto visível + page_source (headless às vezes não expõe tudo em .text)."""
        d = self._d()
        try:
            body = d.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            body = ""
        try:
            src = d.page_source or ""
        except Exception:
            src = ""
        return body + "\n" + src

    def _posicoes_screen_ready(self) -> bool:
        """Detecta tela de Posições (texto, HTML e IDs do Sitrax)."""
        d = self._d()
        blob = self._norm(self._page_blob())

        # marcadores fortes no HTML/texto
        strong = [
            "historico de posic",
            "historico de posicoes",
            "relatorioposicao",
            "idfiltrocveiplaca",  # id do filtro de placa (só existe no fluxo de posições/modal)
            "formmodalsearchveiculo",
            "sbrelatorioposicao",
        ]
        # PT + EN (no Railway o Sitrax abriu em inglês: "Position History")
        if "historico de posic" in blob or "position history" in blob:
            return True

        # menu item marcado
        try:
            el = d.find_element(By.ID, "formTemplate:sbRelatorioPosicao")
            cls = (el.get_attribute("class") or "").lower()
            if "clicked" in cls:
                if "sem conexao" not in blob or "filtrar" in blob or "filter" in blob:
                    if "filtrar" in blob or "filtros" in blob or "filters" in blob or "filter" in blob:
                        return True
        except Exception:
            pass

        has_filtros = "filtros" in blob or "filters" in blob
        has_filtrar = "filtrar" in blob or re.search(r"\bfilter\b", blob)
        has_veiculo_chip = "veiculo" in blob or "vehicle" in blob
        # barra PT/EN: Filtros|Filters · Veículo|Vehicle · Data|Date · Filtrar|Filter
        if has_filtros and has_filtrar and has_veiculo_chip:
            if "ignicao" in blob and "sem conexao" in blob and "ocorrencias" in blob:
                if "historico" not in blob and "position history" not in blob and "data:" not in blob and "date:" not in blob:
                    return False
            return True

        for css in (
            "#itFiltroCveiPlaca",
            "input[id='formModalSearchVeiculo:itCveiPlaca']",
        ):
            try:
                if d.find_elements(By.CSS_SELECTOR, css):
                    return True
            except Exception:
                pass

        for xp in (
            "//*[contains(.,'Histórico de Posições') or contains(.,'Historico de Posicoes') or contains(.,'Position History')]",
            "//*[normalize-space()='Filtros' or normalize-space()='Filters']",
            "//button[contains(.,'Filtrar') or contains(.,'Filter')]",
        ):
            try:
                els = d.find_elements(By.XPATH, xp)
                if any(e.is_displayed() for e in els):
                    if "filtrar" in blob or "filtros" in blob or "filter" in blob or "filters" in blob:
                        return True
            except Exception:
                continue

        return False

    def _jsf_click_posicoes(self) -> bool:
        """Navega via JSF/Mojarra + força visibilidade do item no menu."""
        d = self._d()
        try:
            ok = d.execute_script(
                """
                try {
                  // força sidebar e item visíveis
                  var m = document.getElementById('sidebar-menu');
                  if (m) {
                    m.classList.remove('-translate-x-full');
                    m.style.transform = 'translateX(0px)';
                    m.style.visibility = 'visible';
                    m.style.display = 'block';
                    m.style.opacity = '1';
                  }
                  var el = document.getElementById('formTemplate:sbRelatorioPosicao');
                  if (el) {
                    el.style.display = 'block';
                    el.style.visibility = 'visible';
                    el.removeAttribute('disabled');
                  }
                  var form = document.getElementById('formTemplate');
                  if (form && typeof mojarra !== 'undefined' && mojarra.jsfcljs) {
                    mojarra.jsfcljs(form, {
                      'formTemplate:sbRelatorioPosicao': 'formTemplate:sbRelatorioPosicao'
                    }, '');
                    return 'mojarra';
                  }
                  if (el) {
                    el.click();
                    return 'click';
                  }
                  // procura por texto Posições
                  var links = document.querySelectorAll('a.swNavBarContentButton');
                  for (var i=0;i<links.length;i++) {
                    if ((links[i].textContent||'').indexOf('Posi') >= 0) {
                      links[i].click();
                      return 'text';
                    }
                  }
                  return false;
                } catch (e) {
                  return 'err:' + String(e);
                }
                """
            )
            logger.info("JSF navigate Posições: %s", ok)
            return bool(ok) and not str(ok).startswith("err") and ok is not False
        except Exception as e:
            logger.warning("JSF navigate falhou: %s", e)
            return False

    def open_posicoes(self) -> None:
        """Navega para Históricos → Posições (JSF: formTemplate:sbRelatorioPosicao)."""
        d = self._d()
        self._sleep(2)
        self._trace("posicoes_inicio", "Iniciando navegação para Posições")

        if self._posicoes_screen_ready():
            logger.info("Já está na tela de Posições")
            self._trace("posicoes_ja_aberta", "Já estava em Posições")
            return

        last_err = None
        for attempt in range(3):
            logger.info("Abrindo Posições (tentativa %s/3)", attempt + 1)
            self._trace("posicoes_tentativa", f"Tentativa {attempt + 1}/3")
            self._open_side_menu()
            self._sleep(0.8)

            try:
                btn_hist = d.find_element(By.ID, "btnHistorico")
                d.execute_script("arguments[0].click();", btn_hist)
                self._sleep(0.6)
            except Exception:
                self._click_by_text(
                    ["HISTÓRICOS", "Históricos", "Historicos"], timeout=3
                )

            ok = self._jsf_click_posicoes()
            if not ok:
                ok = self._js_click_id("formTemplate:sbRelatorioPosicao")
            if not ok:
                ok = self._click_by_text(["Posições", "Posicoes"], timeout=5)

            if not ok:
                last_err = "botão Posições não encontrado"
                self._save_debug(f"posicoes_id_tentativa_{attempt}")
                self._sleep(1.5)
                continue

            # postback JSF
            self._sleep(3)
            self._wait_loader_gone(60)
            self._sleep(2)

            try:
                d.execute_script(
                    "if (typeof swCloseSidebar === 'function') { swCloseSidebar(); }"
                )
            except Exception:
                pass

            for _ in range(25):
                if self._posicoes_screen_ready():
                    logger.info("Tela de Posições aberta")
                    self._save_debug("posicoes_ok", "Histórico de Posições confirmado")
                    return
                self._sleep(0.5)

            # fallback otimista: se o menu ficou "Clicked", tenta seguir o fluxo
            try:
                el = d.find_element(By.ID, "formTemplate:sbRelatorioPosicao")
                cls = el.get_attribute("class") or ""
                if "Clicked" in cls or "clicked" in cls:
                    logger.warning(
                        "Menu Posições marcado como clicado; seguindo mesmo sem título visível"
                    )
                    self._save_debug("posicoes_optimistic")
                    return
            except Exception:
                pass

            last_err = "tela não confirmada após clique"
            self._save_debug(f"posicoes_nao_confirmada_{attempt}")
            try:
                d.refresh()
                self._sleep(4)
            except Exception:
                pass

        # última chance: segue e deixa open_vehicle_selector validar
        logger.warning(
            "Não confirmei Posições (%s); tentando continuar o fluxo mesmo assim",
            last_err,
        )
        self._save_debug("posicoes_force_continue")
        # não levanta erro aqui — se estiver errado, falha no botão Veículo com mensagem clara

    def _close_date_popup_if_open(self) -> None:
        """Fecha o popup 'Filtro Data' / 'Date Filter' se estiver aberto."""
        d = self._d()
        try:
            popups = d.find_elements(
                By.XPATH,
                "//*[contains(.,'Filtro Data') or contains(.,'Filtro data') "
                "or contains(.,'Date Filter') or contains(.,'Date filter')]",
            )
            if not any(p.is_displayed() for p in popups[:12]):
                return
            for xp in [
                "//button[normalize-space()='Fechar' or normalize-space()='Close']",
                "//a[normalize-space()='Fechar' or normalize-space()='Close']",
                "//*[normalize-space()='Fechar' or normalize-space()='Close']",
            ]:
                for el in d.find_elements(By.XPATH, xp):
                    try:
                        if not el.is_displayed():
                            continue
                        # só se está no contexto do popup de data
                        try:
                            anc = el.find_element(
                                By.XPATH,
                                "./ancestor::*[contains(.,'Filtro Data') or contains(.,'Date Filter')][1]",
                            )
                            if not anc:
                                continue
                        except Exception:
                            continue
                        self._click(el)
                        self._sleep(0.4)
                        logger.info("Popup de data fechado")
                        return
                    except Exception:
                        continue
            from selenium.webdriver.common.keys import Keys

            d.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            self._sleep(0.3)
        except Exception:
            pass

    def open_vehicle_selector(self) -> None:
        """
        Clica no chip 'Veículo' da barra de filtros (não em Data).
        No headless usa JS (is_displayed() falha com frequência no Railway).
        """
        d = self._d()
        self._sleep(0.8)
        self._close_date_popup_if_open()

        # 1) Clique via JavaScript no DOM inteiro (inclui nós "invisíveis" ao Selenium)
        clicked = False
        try:
            result = d.execute_script(
                """
                function norm(s) {
                  return (s || '').replace(/\\s+/g, ' ').trim();
                }
                var nodes = document.querySelectorAll('a,button,span,div,label,li,p');
                var candidates = [];
                for (var i = 0; i < nodes.length; i++) {
                  var el = nodes[i];
                  if (el.closest && (el.closest('#sidebar-menu') || el.closest('#sidebar'))) continue;
                  var t = norm(el.innerText || el.textContent);
                  if (!t) continue;
                  // PT: Veículo / EN: Vehicle (Sitrax no Railway usa inglês)
                  var isVeic =
                    t === 'Veículo' || t === 'Veiculo' || t === 'Vehicle' ||
                    /^Ve[ií]culo\\s*:/.test(t) || /^Vehicle\\s*:/i.test(t) ||
                    (t.indexOf('Veículo') === 0 && t.length <= 28) ||
                    (t.indexOf('Veiculo') === 0 && t.length <= 28) ||
                    (t.indexOf('Vehicle') === 0 && t.length <= 28);
                  if (!isVeic) continue;
                  var r = el.getBoundingClientRect();
                  if (r.height > 100 || r.width > 500) continue;
                  if (/^(Data|Date)\\b/i.test(t) || t.indexOf('Data:') === 0 || t.indexOf('Date:') === 0) continue;
                  candidates.push({el: el, t: t, w: r.width || 9999});
                }
                candidates.sort(function(a,b){ return a.w - b.w; });
                if (candidates.length) {
                  var c = candidates[0].el;
                  c.scrollIntoView({block:'center'});
                  c.click();
                  return candidates[0].t;
                }
                return null;
                """
            )
            if result:
                clicked = True
                logger.info("Clicou em Vehicle/Veículo via JS: %s", result)
        except Exception as e:
            logger.warning("JS clique Veículo/Vehicle: %s", e)

        # 2) Fallback Selenium clássico (PT + EN)
        if not clicked:
            for el in d.find_elements(
                By.XPATH,
                "//*[contains(text(),'Veículo') or contains(text(),'Veiculo') or contains(text(),'Vehicle')]",
            ):
                try:
                    t = (el.text or "").strip()
                    if not t or t.startswith("Data") or t.startswith("Date"):
                        continue
                    if not any(x in t for x in ("Veículo", "Veiculo", "Vehicle")):
                        continue
                    if len(t) > 30:
                        continue
                    d.execute_script("arguments[0].click();", el)
                    clicked = True
                    logger.info("Clicou Vehicle/Veículo fallback: %s", t)
                    break
                except Exception:
                    continue

        if not clicked:
            self._save_debug(
                "veiculo_botao_nao_encontrado",
                "Botão Vehicle/Veículo NÃO encontrado — veja a foto no painel /debug",
                ok=False,
            )
            raise TimeoutException(
                "Não encontrou o botão 'Vehicle'/'Veículo' na barra de filtros. "
                "Abra /debug para ver a tela que o robô enxergou."
            )

        self._wait_loader_gone(25)
        self._sleep(1.0)

        # modal "Selecione Veículo" / "Select Vehicle" ou input de placa
        end = time.time() + 20
        modal_ok = False
        while time.time() < end:
            blob = self._norm(self._page_blob())
            if (
                "selecione veiculo" in blob
                or "select vehicle" in blob
                or "formmodalsearchveiculo" in blob
            ):
                modal_ok = True
                break
            if d.find_elements(
                By.CSS_SELECTOR,
                "#itFiltroCveiPlaca, input[id='formModalSearchVeiculo:itCveiPlaca']",
            ):
                modal_ok = True
                break
            self._sleep(0.4)

        if not modal_ok:
            self._save_debug("veiculo_modal_nao_abriu")
            blob = self._page_blob()
            if "Filtro Data" in blob or "Date Filter" in blob or "Filter Date" in blob:
                raise TimeoutException("Abriu filtro de DATA em vez de VEÍCULO/Vehicle.")
            raise TimeoutException(
                "Clicou em Vehicle/Veículo mas o modal não abriu. "
                f"Veja {DEBUG_DIR}"
            )
        logger.info("Modal de veículos aberto")

    def _count_vehicle_items(self) -> int:
        """Conta itens do modal (cadVeiculoSearchSelect) — sem depender de is_displayed."""
        d = self._d()
        try:
            n = d.execute_script(
                """
                var a = document.querySelectorAll("div[onclick*='cadVeiculoSearchSelect']");
                if (a && a.length) return a.length;
                var b = document.querySelectorAll("div[id$='_idDivSearchVeiculo']");
                return b ? b.length : 0;
                """
            )
            return int(n or 0)
        except Exception:
            pass
        try:
            return len(
                d.find_elements(
                    By.CSS_SELECTOR, "div[onclick*='cadVeiculoSearchSelect']"
                )
            )
        except Exception:
            return 0

    def _vehicle_rows_visible(self) -> bool:
        """True se o modal já tem veículos na lista."""
        n = self._count_vehicle_items()
        if n >= 1:
            return True
        # fallback: placa no HTML do modal
        try:
            src = self._d().page_source or ""
            if "cadVeiculoSearchSelect" in src and re.search(
                r"[A-Z]{3}\d[A-Z0-9]\d{2}", src
            ):
                return True
        except Exception:
            pass
        return False

    def _click_placa_lupa_preta(self) -> bool:
        """
        Clica na lupa PRETA ao lado de Placa.

        HTML real do Sitrax:
          <div id="itFiltroCveiPlaca">
            <input id="formModalSearchVeiculo:itCveiPlaca" placeholder="Placa">
            <i class="fa-solid fa-magnifying-glass ... bg-white ..."
               onclick="swClick('formModalSearchVeiculo:btnFiltrarVeiculo')"></i>
          </div>

        NÃO clicar nas lupas roxas de Display / Cliente / Serial.
        """
        d = self._d()

        # 1) seletor exato da lupa preta (bg-white) dentro de itFiltroCveiPlaca
        selectors = [
            (By.CSS_SELECTOR, "#itFiltroCveiPlaca i.fa-magnifying-glass"),
            (By.CSS_SELECTOR, "#itFiltroCveiPlaca i.fa-solid.fa-magnifying-glass"),
            (By.CSS_SELECTOR, "div#itFiltroCveiPlaca i[onclick*=\"btnFiltrarVeiculo\"]"),
            (By.XPATH, "//div[@id='itFiltroCveiPlaca']//i[contains(@class,'fa-magnifying-glass')]"),
            (By.XPATH, "//input[@id='formModalSearchVeiculo:itCveiPlaca']/following-sibling::i[contains(@class,'magnifying')]"),
            (By.XPATH, "//input[@placeholder='Placa' or @placeholder='Plate' or @placeholder='placa']/following-sibling::i[contains(@class,'magnifying')]"),
        ]
        for by, sel in selectors:
            try:
                els = d.find_elements(by, sel)
            except Exception:
                continue
            for el in els:
                try:
                    # garante que NÃO é coluna Display/Cliente/Serial
                    try:
                        parent = el.find_element(
                            By.XPATH,
                            "./ancestor::div[contains(@id,'Filtro') or contains(@id,'filtro')][1]",
                        )
                        pid = (parent.get_attribute("id") or "").lower()
                        if any(x in pid for x in ("display", "cemp", "cequ", "serial", "cliente")):
                            continue
                    except NoSuchElementException:
                        pass

                    d.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", el
                    )
                    # clique via onclick nativo (mais confiável no Sitrax)
                    oc = el.get_attribute("onclick") or ""
                    if "swClick" in oc or "btnFiltrarVeiculo" in oc:
                        d.execute_script(
                            "if (typeof swClick === 'function') {"
                            "  swClick('formModalSearchVeiculo:btnFiltrarVeiculo');"
                            "} else { arguments[0].click(); }",
                            el,
                        )
                    else:
                        d.execute_script("arguments[0].click();", el)
                    logger.info("Clicou na lupa PRETA de Placa (%s)", sel)
                    return True
                except Exception as e:
                    logger.warning("Tentativa lupa Placa falhou: %s", e)

        # 2) fallback: só o JS do botão de filtrar (lista completa, sem digitar)
        try:
            d.execute_script(
                "if (typeof swClick === 'function') {"
                "  swClick('formModalSearchVeiculo:btnFiltrarVeiculo');"
                "  return true;"
                "} return false;"
            )
            logger.info("Disparou swClick(btnFiltrarVeiculo) sem lupa")
            return True
        except Exception as e:
            logger.warning("swClick filtrar falhou: %s", e)
        return False

    def _filter_modal_by_plate(self, placa_u: str) -> bool:
        """Digita a placa no filtro do modal e clica Filtrar (JSF)."""
        d = self._d()
        placa_u = re.sub(r"[^A-Z0-9]", "", (placa_u or "").upper())
        if not placa_u:
            return False
        try:
            inp = None
            for sel in (
                "input[id='formModalSearchVeiculo:itCveiPlaca']",
                "input[id*='itCveiPlaca']",
                "input[id*='Placa']",
            ):
                try:
                    els = d.find_elements(By.CSS_SELECTOR, sel)
                    for el in els:
                        if el.is_displayed() or True:
                            inp = el
                            break
                except Exception:
                    continue
                if inp is not None:
                    break
            if inp is None:
                return False
            d.execute_script(
                "arguments[0].scrollIntoView({block:'center'});"
                "arguments[0].value='';"
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                inp,
                placa_u,
            )
            try:
                inp.clear()
                inp.send_keys(placa_u)
            except Exception:
                pass
            # botão filtrar do modal
            clicked = False
            try:
                d.execute_script(
                    "if (typeof swClick === 'function') "
                    "swClick('formModalSearchVeiculo:btnFiltrarVeiculo');"
                )
                clicked = True
            except Exception:
                pass
            if not clicked:
                for xp in (
                    "//button[contains(@id,'btnFiltrarVeiculo')]",
                    "//button[contains(.,'Filter') or contains(.,'Filtrar')]",
                    "//a[contains(@id,'btnFiltrarVeiculo')]",
                ):
                    try:
                        for btn in d.find_elements(By.XPATH, xp):
                            d.execute_script("arguments[0].click();", btn)
                            clicked = True
                            break
                    except Exception:
                        continue
                    if clicked:
                        break
            self._wait_loader_gone(30)
            self._sleep(0.8)
            return True
        except Exception as e:
            logger.warning("Filtro modal placa %s: %s", placa_u, e)
            return False

    @staticmethod
    def _norm_placa(s: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

    # Mercosul ABC1D23 | antiga ABC1234
    _PLACA_TOKEN_RE = re.compile(r"^[A-Z]{3}\d[A-Z0-9]\d{2}$|^[A-Z]{3}\d{4}$", re.I)
    _PLACA_SEARCH_RE = re.compile(
        r"\b([A-Z]{3}\d[A-Z0-9]\d{2}|[A-Z]{3}\d{4})\b", re.I
    )

    def _extract_plate_token(self, text: str) -> str:
        """Extrai a 1ª placa canônica de onclick/texto (match exato de token)."""
        if not text:
            return ""
        # Preferir args de cadVeiculoSearchSelect('PLACA', ...)
        m = re.search(
            r"cadVeiculoSearchSelect\s*\(\s*['\"]([A-Za-z0-9\-]+)['\"]",
            text,
            re.I,
        )
        if m:
            return self._norm_placa(m.group(1))
        # Aspas soltas com formato de placa
        for m in re.finditer(r"['\"]([A-Za-z0-9\-]{5,10})['\"]", text):
            p = self._norm_placa(m.group(1))
            if self._PLACA_TOKEN_RE.match(p):
                return p
        m2 = self._PLACA_SEARCH_RE.search((text or "").upper())
        if m2:
            return self._norm_placa(m2.group(1))
        return ""

    def _item_plate(self, el) -> str:
        """Placa da linha do modal (onclick > spans > texto)."""
        try:
            oc = el.get_attribute("onclick") or ""
            p = self._extract_plate_token(oc)
            if p:
                return p
        except Exception:
            pass
        try:
            texts = []
            for s in el.find_elements(By.CSS_SELECTOR, "span.swMiniModalItemsText, span"):
                t = (s.text or "").strip()
                if t:
                    texts.append(t)
            blob = " ".join(texts) if texts else (el.text or "")
            return self._extract_plate_token(blob)
        except Exception:
            return ""

    def _scroll_modal_vehicle_list(self) -> None:
        """Rola o corpo da lista do modal para carregar itens no fim da frota."""
        d = self._d()
        try:
            d.execute_script(
                """
                var sels = [
                  "div[onclick*='cadVeiculoSearchSelect']",
                  "div.swModalContentListItem",
                  "div[id$='_idDivSearchVeiculo']"
                ];
                var first = null;
                for (var s = 0; s < sels.length && !first; s++) {
                  first = document.querySelector(sels[s]);
                }
                if (!first) return;
                var box = first.parentElement;
                for (var i = 0; i < 8 && box; i++) {
                  var st = window.getComputedStyle(box);
                  var oy = st && st.overflowY;
                  if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay')
                      && box.scrollHeight > box.clientHeight + 8) {
                    box.scrollTop = box.scrollHeight;
                    return;
                  }
                  box = box.parentElement;
                }
                try { first.scrollIntoView({block:'end'}); } catch(e) {}
                """
            )
        except Exception:
            pass

    def load_vehicle_list(self, placa: Optional[str] = None) -> None:
        """
        Abre/preenche a lista do modal de veículos.
          - Sem placa: garante lista carregada (para frota / list_plates).
          - Com placa: SEMPRE filtra digitando a placa e SÓ retorna se a
            linha exata existir (nunca aceita "lista com qualquer item").
        """
        self._sleep(0.6)
        placa_u = self._norm_placa(placa or "")

        def _ensure_list_or_lupa() -> None:
            for _ in range(12):
                if self._count_vehicle_items() >= 1:
                    return
                self._sleep(0.4)
            self._trace(
                "lista_vazia_vai_lupa",
                "Lista vazia após espera; clicando lupa de Placa UMA vez",
                ok=True,
                shot=True,
            )
            logger.info("Lista vazia; clicando lupa preta de Placa (1x)…")
            if not self._click_placa_lupa_preta():
                self._save_debug(
                    "lupa_preta_placa_nao_achada",
                    "Lupa de Placa não encontrada",
                    ok=False,
                )
                raise TimeoutException(
                    "Lista vazia e não achei a lupa preta ao lado de Placa. "
                    "Abra /debug."
                )
            self._wait_loader_gone(45)
            self._sleep(1.0)
            end = time.time() + 25
            while time.time() < end:
                n = self._count_vehicle_items()
                if n >= 1:
                    self._save_debug(
                        "lista_apos_lupa_placa",
                        f"Veículos após lupa Placa: {n} item(ns)",
                        ok=True,
                    )
                    return
                self._sleep(0.4)
            self._save_debug(
                "lista_vazia_apos_lupa_placa",
                "Lista continua vazia após lupa",
                ok=False,
            )
            raise TimeoutException(
                "Modal aberto mas a lista de veículos não carregou. "
                "Abra /debug para calibrar."
            )

        _ensure_list_or_lupa()
        n0 = self._count_vehicle_items()
        self._trace(
            "lista_ja_pronta",
            f"Lista pronta com {n0} veículo(s)",
            ok=True,
            shot=True,
        )

        if not placa_u:
            return

        # Com placa alvo: filtrar SEMPRE (lista suja da placa anterior)
        # e exigir match EXATO — não basta count >= 1.
        for attempt in range(2):
            self._trace(
                "filtra_placa_modal",
                f"Filtrando modal por {placa_u} (tentativa {attempt + 1})",
                ok=True,
                shot=True,
            )
            self._filter_modal_by_plate(placa_u)
            self._sleep(0.5)
            # rola lista filtrada (última da frota às vezes só no fim)
            for _ in range(4):
                if self._find_vehicle_item(placa_u) is not None:
                    self._trace(
                        "placa_na_lista_apos_filtro",
                        f"{placa_u} encontrada na linha do modal",
                        ok=True,
                    )
                    return
                self._scroll_modal_vehicle_list()
                self._sleep(0.35)

        self._save_debug(f"placa_nao_na_lista_{placa_u}", ok=False)
        sample = ""
        try:
            sample = ", ".join(v["placa"] for v in self.list_plates()[:15])
        except Exception:
            pass
        raise NoSuchElementException(
            f"Placa {placa_u} não apareceu na lista do modal após filtrar. "
            f"Amostra: {sample}. Veja {DEBUG_DIR}"
        )

    def list_plates(self) -> list[dict]:
        """
        Lista placas do modal (cadVeiculoSearchSelect / swModalContentListItem).
        Rola a lista para não perder as últimas placas da frota.
        """
        d = self._d()
        by_placa: dict[str, dict] = {}

        def _harvest() -> None:
            items = d.find_elements(
                By.CSS_SELECTOR,
                "div[onclick*='cadVeiculoSearchSelect'], div.swModalContentListItem",
            )
            for i, item in enumerate(items):
                try:
                    placa = self._item_plate(item)
                    if not placa or placa in by_placa:
                        continue
                    texts = [
                        s.text.strip()
                        for s in item.find_elements(
                            By.CSS_SELECTOR, "span.swMiniModalItemsText"
                        )
                        if (s.text or "").strip()
                    ]
                    by_placa[placa] = {
                        "placa": placa,
                        "display": texts[1] if len(texts) > 1 else "",
                        "cliente": texts[2] if len(texts) > 2 else "",
                        "serial": texts[3] if len(texts) > 3 else "",
                        "index": i,
                    }
                except StaleElementReferenceException:
                    continue

        # Várias passagens com scroll — últimas linhas costumam estar no fim
        for pass_i in range(8):
            _harvest()
            before = len(by_placa)
            self._scroll_modal_vehicle_list()
            self._sleep(0.25)
            _harvest()
            if pass_i >= 2 and len(by_placa) == before:
                break

        vehicles = list(by_placa.values())
        # index estável na ordem em que o DOM/scroll revelou
        for i, v in enumerate(vehicles):
            v["index"] = i
        logger.info("Veículos: %s", len(vehicles))
        return vehicles

    def _find_vehicle_item(self, placa_u: str):
        """
        Encontra a LINHA do modal com a placa EXATA.
        Não usa contains frouxo (evita pegar linha errada).
        Rola a lista se a placa estiver no fim.
        """
        d = self._d()
        placa_u = self._norm_placa(placa_u)
        if not placa_u:
            return None

        def _scan():
            # 1) divs com cadVeiculoSearchSelect — match exato no token da placa
            for el in d.find_elements(
                By.CSS_SELECTOR, "div[onclick*='cadVeiculoSearchSelect']"
            ):
                try:
                    if self._item_plate(el) == placa_u:
                        try:
                            d.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});", el
                            )
                        except Exception:
                            pass
                        return el
                except StaleElementReferenceException:
                    continue

            # 2) list item genérico — placa exata no texto normalizado
            for el in d.find_elements(
                By.CSS_SELECTOR,
                "div.swModalContentListItem, div[id$='_idDivSearchVeiculo']",
            ):
                try:
                    if self._item_plate(el) == placa_u:
                        try:
                            d.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});", el
                            )
                        except Exception:
                            pass
                        return el
                except StaleElementReferenceException:
                    continue

            # 3) JS: percorre onclick e devolve o nó com token exato
            try:
                el = d.execute_script(
                    """
                    var alvo = (arguments[0] || '').toUpperCase().replace(/[^A-Z0-9]/g,'');
                    if (!alvo) return null;
                    function norm(s) {
                      return (s || '').toUpperCase().replace(/[^A-Z0-9]/g,'');
                    }
                    function plateFrom(el) {
                      var oc = el.getAttribute('onclick') || '';
                      var m = oc.match(/cadVeiculoSearchSelect\\s*\\(\\s*['\"]([^'\"]+)['\"]/i);
                      if (m) return norm(m[1]);
                      var quotes = oc.match(/['\"]([A-Za-z0-9\\-]{5,10})['\"]/g) || [];
                      for (var q = 0; q < quotes.length; q++) {
                        var t = norm(quotes[q].replace(/['\"]/g,''));
                        if (/^[A-Z]{3}\\d[A-Z0-9]\\d{2}$/.test(t) || /^[A-Z]{3}\\d{4}$/.test(t))
                          return t;
                      }
                      return norm(el.innerText || el.textContent || '');
                    }
                    var nodes = document.querySelectorAll(
                      "div[onclick*='cadVeiculoSearchSelect'], div.swModalContentListItem"
                    );
                    for (var i = 0; i < nodes.length; i++) {
                      var p = plateFrom(nodes[i]);
                      // SOMENTE match exato do token da placa (evita linha errada)
                      if (p === alvo) return nodes[i];
                      // fallback: texto da linha tem a placa como token isolado
                      var raw = norm(nodes[i].innerText || '');
                      if (raw === alvo) return nodes[i];
                      var re = new RegExp('(?:^|[^A-Z0-9])' + alvo + '(?:[^A-Z0-9]|$)');
                      if (re.test(raw) && (p === alvo || !p || p.length > 12)) {
                        // se plateFrom devolveu lixo longo (texto inteiro), aceita token isolado
                        if (p === alvo || p.length > 12) return nodes[i];
                      }
                    }
                    return null;
                    """,
                    placa_u,
                )
                if el is not None:
                    try:
                        d.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", el
                        )
                    except Exception:
                        pass
                    return el
            except Exception as e:
                logger.debug("JS _find_vehicle_item: %s", e)
            return None

        found = _scan()
        if found is not None:
            return found
        # última da lista: rolar e reescanear
        for _ in range(6):
            self._scroll_modal_vehicle_list()
            self._sleep(0.2)
            found = _scan()
            if found is not None:
                return found
        return None

    def select_vehicle_by_plate(self, placa: str) -> dict:
        """
        Seleciona veículo no modal Sitrax via cadVeiculoSearchSelect + checkbox,
        depois selectVeiculoSearch() / botão Selecionar.

        Sempre filtra pela placa e exige linha com token EXATO (últimas da
        frota falhavam com match frouxo / sem scroll).
        """
        d = self._d()
        placa_u = self._norm_placa(placa)
        logger.info("Selecionando placa %s (match exato + filtro modal)", placa_u)

        # 1) SEMPRE filtrar pela placa (lista suja / placa no fim)
        self._filter_modal_by_plate(placa_u)
        self._sleep(0.5)

        item = None
        end = time.time() + 22
        while time.time() < end:
            item = self._find_vehicle_item(placa_u)
            if item is not None:
                break
            self._scroll_modal_vehicle_list()
            time.sleep(0.35)

        if item is None:
            # 2ª chance: limpa filtro (lupa) e filtra de novo
            try:
                self._click_placa_lupa_preta()
                self._wait_loader_gone(30)
                self._sleep(0.5)
            except Exception:
                pass
            self._filter_modal_by_plate(placa_u)
            self._sleep(0.6)
            item = self._find_vehicle_item(placa_u)

        if item is None:
            self._save_debug(f"placa_nao_encontrada_{placa_u}")
            try:
                n = self._count_vehicle_items()
                sample = ", ".join(v["placa"] for v in self.list_plates()[:20])
            except Exception:
                n, sample = "?", ""
            raise NoSuchElementException(
                f"Placa {placa_u} não encontrada no modal "
                f"({n} itens). Amostra: {sample}. "
                f"Veja {DEBUG_DIR}"
            )

        # Confirma que a linha é realmente a placa pedida
        got = self._item_plate(item)
        if got and got != placa_u:
            self._save_debug(f"placa_mismatch_{placa_u}_vs_{got}", ok=False)
            raise NoSuchElementException(
                f"Linha errada no modal: pedi {placa_u}, achei {got}."
            )

        # --- marcar via função nativa do Sitrax ---
        selected_ok = False
        try:
            oc = item.get_attribute("onclick") or ""
            tok = self._extract_plate_token(oc) if oc else ""
            # só executa onclick se o token for a placa pedida (ou ilegível)
            if oc and tok in ("", placa_u):
                d.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", item
                )
                d.execute_script(oc if oc.strip().endswith(";") else oc + ";")
                selected_ok = True
                logger.info(
                    "Executou cadVeiculoSearchSelect via onclick para %s", placa_u
                )
            else:
                d.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].click();",
                    item,
                )
                selected_ok = True
        except Exception as e:
            logger.warning("onclick falhou (%s); tentando click no div", e)
            try:
                d.execute_script("arguments[0].click();", item)
                selected_ok = True
            except Exception:
                try:
                    self._click(item)
                    selected_ok = True
                except Exception as e2:
                    logger.warning("click div falhou: %s", e2)

        # marca checkbox se existir
        try:
            cb = item.find_element(
                By.CSS_SELECTOR, "input[type='checkbox'], input.swCheckBoxCustom"
            )
            d.execute_script(
                """
                var c = arguments[0];
                c.checked = true;
                c.click();
                c.dispatchEvent(new Event('change', {bubbles:true}));
                """,
                cb,
            )
            if not cb.is_selected():
                d.execute_script("arguments[0].checked = true;", cb)
            selected_ok = selected_ok or cb.is_selected()
            logger.info("Checkbox %s checked=%s", placa_u, cb.is_selected())
        except NoSuchElementException:
            pass
        except Exception as e:
            logger.warning("checkbox: %s", e)

        if not selected_ok:
            self._save_debug(f"nao_marcou_{placa_u}")
            raise TimeoutException(
                f"Não consegui marcar {placa_u}. Não clico em Selecionar. "
                f"Veja {DEBUG_DIR}"
            )

        self._sleep(0.5)

        # --- Selecionar: função nativa preferencial ---
        try:
            d.execute_script(
                "if (typeof selectVeiculoSearch === 'function') { selectVeiculoSearch(); }"
                "if (typeof hideModalSearchVeiculo === 'function') { hideModalSearchVeiculo(); }"
            )
            logger.info("Chamou selectVeiculoSearch() + hideModalSearchVeiculo()")
        except Exception as e:
            logger.warning("JS selectVeiculoSearch falhou: %s", e)

        sel = None
        for el in d.find_elements(
            By.XPATH,
            "//*[contains(@onclick,'selectVeiculoSearch')] | "
            "//button[contains(.,'Selecionar') or contains(.,'Select')] | "
            "//a[contains(.,'Selecionar') or contains(.,'Select')] | "
            "//span[normalize-space()='Selecionar' or normalize-space()='Select']"
            "/ancestor::*[self::button or self::a or self::div][1]",
        ):
            try:
                if el.is_displayed():
                    sel = el
                    break
            except Exception:
                continue

        if sel is not None:
            try:
                d.execute_script("arguments[0].click();", sel)
            except Exception:
                try:
                    self._click(sel)
                except Exception:
                    pass

        self._wait_loader_gone(25)
        self._sleep(1.0)

        # modal deve sumir (senão o Filter da barra some / erra)
        try:
            body = d.find_element(By.TAG_NAME, "body").text
        except Exception:
            body = ""
        if (
            "Selecione Veículo" in body
            or "Selecione Veiculo" in body
            or "Select Vehicle" in body
        ):
            try:
                d.execute_script(
                    "if (typeof hideModalSearchVeiculo === 'function') hideModalSearchVeiculo();"
                )
                self._sleep(0.5)
            except Exception:
                pass
            try:
                # Escape / clique fora
                from selenium.webdriver.common.keys import Keys

                d.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:
                pass
            body = d.find_element(By.TAG_NAME, "body").text
            if "Selecione Veículo" in body or "Select Vehicle" in body:
                self._save_debug(f"modal_ainda_aberto_{placa_u}")
                logger.warning("Modal ainda aberto após Select/Selecionar")

        logger.info("Veículo %s selecionado com sucesso", placa_u)
        self._save_debug(f"veiculo_ok_{placa_u}")
        return {"placa": placa_u}

    # —— Filtro de data Sitrax (chip → popup → calendário range) ——

    _MES_PT = (
        "janeiro",
        "fevereiro",
        "março",
        "abril",
        "maio",
        "junho",
        "julho",
        "agosto",
        "setembro",
        "outubro",
        "novembro",
        "dezembro",
    )
    _MES_EN = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )

    def _read_date_chip_text(self) -> str:
        """Lê o chip Data/Date da barra (PT ou EN)."""
        d = self._d()
        try:
            chip = d.execute_script(
                """
                function norm(s){ return (s||'').replace(/\\s+/g,' ').trim(); }
                var best = '', bestScore = 1e9;
                var nodes = document.querySelectorAll('*');
                for (var i=0;i<nodes.length;i++){
                  var el = nodes[i];
                  if (el.children && el.children.length > 8) continue;
                  var t = norm(el.innerText || el.textContent);
                  if (!t || t.length < 10 || t.length > 140) continue;
                  if (!/\\d{2}\\/\\d{2}\\/\\d{4}/.test(t)) continue;
                  // Date: ... Until ...  OU  Data: ... Até ...
                  var isChip = /\\bDate\\s*:/i.test(t) || /\\bData\\s*:/i.test(t);
                  if (!isChip) continue;
                  if (/Ve[ií]culo|Vehicle|Mostrando|Showing|Filtro\\s*Data|Date\\s*Filter/i.test(t)
                      && !/^\\s*(Data|Date)\\s*:/i.test(t)) continue;
                  // preferir nós curtos só com o chip
                  if (/GPS|Parked|Normal|Estacionado|Registro/i.test(t)) continue;
                  var r = el.getBoundingClientRect();
                  if (r.width < 30 || r.height < 5) continue;
                  if (r.top > 350) continue;
                  var score = t.length + r.top * 0.01;
                  if (score < bestScore){ bestScore = score; best = t; }
                }
                return best;
                """
            )
            if chip:
                return re.sub(r"\s+", " ", str(chip)).strip()
        except Exception:
            pass
        return ""

    def _date_chip_matches(self, data_ini: date, data_fim: date) -> bool:
        chip = self._read_date_chip_text()
        if not chip:
            return False
        dates = re.findall(r"\d{2}/\d{2}/\d{4}", chip)
        if not dates:
            return False
        ini_br = data_ini.strftime("%d/%m/%Y")
        fim_br = data_fim.strftime("%d/%m/%Y")
        if len(dates) == 1:
            return data_ini == data_fim and dates[0] == ini_br
        return dates[0] == ini_br and dates[-1] == fim_br

    def _click_date_chip(self) -> bool:
        """
        Clica no chip 'Data:' / 'Date:' da barra de filtros.
        (ex.: Date: 11/07/2026 00:00:00 Until 11/07/2026 23:59:59)
        """
        d = self._d()
        try:
            clicked = d.execute_script(
                """
                function norm(s){ return (s||'').replace(/\\s+/g,' ').trim(); }
                var candidates = [];
                var nodes = document.querySelectorAll('a,span,div,button,label,li,p,td');
                for (var i=0;i<nodes.length;i++){
                  var el = nodes[i];
                  var t = norm(el.innerText || el.textContent);
                  if (!t || t.length < 8 || t.length > 140) continue;
                  if (!/\\d{2}\\/\\d{2}\\/\\d{4}/.test(t)) continue;
                  if (!(/\\bDate\\s*:/i.test(t) || /\\bData\\s*:/i.test(t))) continue;
                  if (/Ve[ií]culo\\s*:|Vehicle\\s*:/i.test(t)) continue;
                  if (/Filtro\\s*Data|Date\\s*Filter/i.test(t) && t.length > 80) continue;
                  if (/Mostrando|Showing|Parked|Estacionado|GPS Date/i.test(t)) continue;
                  var r = el.getBoundingClientRect();
                  if (r.width < 40 || r.height < 6 || r.height > 120) continue;
                  if (r.top > 400 || r.bottom < 0) continue;
                  // preferir o menor nó (folha) que ainda tem o texto do chip
                  candidates.push({el:el, t:t, len:t.length, top:r.top});
                }
                candidates.sort(function(a,b){
                  return (a.len - b.len) || (a.top - b.top);
                });
                if (!candidates.length) return {ok:false, n:0};
                var c = candidates[0];
                try { c.el.scrollIntoView({block:'center', inline:'nearest'}); } catch(e){}
                c.el.click();
                return {ok:true, t:c.t, n:candidates.length};
                """
            )
            if clicked and clicked.get("ok"):
                logger.info("Clicou chip Data/Date: %s", clicked.get("t"))
                return True
            logger.warning("Chip Data/Date JS: %s", clicked)
        except Exception as e:
            logger.warning("JS click chip data: %s", e)

        # Selenium: qualquer elemento com Date: / Data: + data
        for el in d.find_elements(
            By.XPATH,
            "//*[contains(.,'Date:') or contains(.,'Data:')]",
        ):
            try:
                t = re.sub(r"\s+", " ", (el.text or "").strip())
                if len(t) > 140 or len(t) < 8:
                    continue
                if not re.search(r"\d{2}/\d{2}/\d{4}", t):
                    continue
                if not (re.search(r"\bDate\s*:", t, re.I) or re.search(r"\bData\s*:", t, re.I)):
                    continue
                if re.search(r"Vehicle\s*:|Ve[ií]culo\s*:", t, re.I):
                    continue
                if not el.is_displayed():
                    continue
                self._click(el)
                logger.info("Clicou chip Data (selenium): %s", t[:90])
                return True
            except Exception:
                continue
        return False

    def _wait_filtro_data_popup(self, timeout: float = 4.0) -> bool:
        """Espera o popup 'Filtro Data' / 'Date Filter'."""
        d = self._d()
        end = time.time() + timeout
        while time.time() < end:
            try:
                body = d.find_element(By.TAG_NAME, "body").text or ""
                if (
                    "Filtro Data" in body
                    or "Date Filter" in body
                    or "Filtro data" in body
                    or ("Início" in body and "Fim" in body)
                    or ("Inicio" in body and "Fim" in body)
                    or ("Start" in body and "End" in body)
                ):
                    return True
            except Exception:
                pass
            self._sleep(0.2)
        return False

    def _click_popup_filtrar(self) -> bool:
        """Clica Filtrar/Filter DENTRO do popup Filtro Data."""
        d = self._d()
        try:
            ok = d.execute_script(
                """
                function norm(s){ return (s||'').replace(/\\s+/g,' ').trim(); }
                var nodes = document.querySelectorAll('button,a,input[type=button],input[type=submit],span');
                var best = null;
                for (var i=0;i<nodes.length;i++){
                  var el = nodes[i];
                  var t = norm(el.innerText || el.value || '');
                  if (t !== 'Filtrar' && t !== 'Filter') continue;
                  var r = el.getBoundingClientRect();
                  if (r.width < 20 || r.height < 10) continue;
                  // contexto: popup de data (não o Filter grande da barra)
                  var p = el, ctx = '';
                  for (var k=0;k<7 && p;k++){
                    ctx += ' ' + norm(p.innerText||'').slice(0,150);
                    p = p.parentElement;
                  }
                  if (!/Filtro\\s*Data|Date\\s*Filter|In[ií]cio|Start\\s*:/i.test(ctx)) continue;
                  // botão pequeno do popup (barra tem botão maior com ícone)
                  if (r.width > 220) continue;
                  best = el;
                  break;
                }
                if (!best){
                  // 2º: qualquer Filtrar perto de Início/Fim
                  for (var i=0;i<nodes.length;i++){
                    var el = nodes[i];
                    var t = norm(el.innerText || el.value || '');
                    if (t !== 'Filtrar' && t !== 'Filter') continue;
                    var p = el, ctx = '';
                    for (var k=0;k<6 && p;k++){
                      ctx += ' ' + norm(p.innerText||'').slice(0,120);
                      p = p.parentElement;
                    }
                    if (/In[ií]cio|Filtro\\s*Data|Date\\s*Filter/i.test(ctx)){
                      best = el; break;
                    }
                  }
                }
                if (!best) return false;
                best.click();
                return true;
                """
            )
            if ok:
                logger.info("Clicou Filtrar do popup de data")
                return True
        except Exception as e:
            logger.warning("click popup filtrar: %s", e)
        return False

    def _click_written_date_inicio(self) -> bool:
        """
        Clica na data escrita do popup (Início: 11/07/2026 00:00:00) — roxo.
        Isso abre o calendário de intervalo.
        """
        d = self._d()
        try:
            clicked = d.execute_script(
                """
                function norm(s){ return (s||'').replace(/\\s+/g,' ').trim(); }
                // 1) procura linha Início / Start com data clicável
                var nodes = document.querySelectorAll('span,a,div,td,label,p,b,strong,em,input');
                var targets = [];
                for (var i=0;i<nodes.length;i++){
                  var el = nodes[i];
                  var t = norm(el.innerText || el.value || el.textContent || '');
                  if (!t || t.length > 80) continue;
                  if (!/\\d{2}\\/\\d{2}\\/\\d{4}/.test(t)) continue;
                  // linha Início ou só a data roxa
                  var hasIni = /In[ií]cio|Start/i.test(t);
                  var onlyDate = /^\\d{2}\\/\\d{2}\\/\\d{4}(\\s+\\d{2}:\\d{2}(:\\d{2})?)?$/.test(t);
                  if (!hasIni && !onlyDate) continue;
                  // evita chip da barra (Date: ... Until)
                  if (/\\bDate\\s*:|\\bData\\s*:/i.test(t) && /Until|At[eé]/i.test(t)) continue;
                  if (/Ve[ií]culo|Vehicle|Filtros|Filters/i.test(t)) continue;
                  var r = el.getBoundingClientRect();
                  if (r.width < 8 || r.height < 6) continue;
                  if (r.top < 40 || r.top > 500) continue;
                  var score = (hasIni ? 0 : 10) + t.length;
                  targets.push({el:el, t:t, score:score, top:r.top});
                }
                // preferir Início: ... e o nó mais curto
                targets.sort(function(a,b){ return a.score - b.score || a.top - b.top; });
                if (!targets.length) return {ok:false};
                // se há Início, clica no trecho da data (filho) se existir
                var c = targets[0];
                var clickEl = c.el;
                // filhos com só a data
                var kids = c.el.querySelectorAll('span,a,div,b,em,input');
                for (var j=0;j<kids.length;j++){
                  var kt = norm(kids[j].innerText || kids[j].value || '');
                  if (/^\\d{2}\\/\\d{2}\\/\\d{4}/.test(kt) && kt.length < 30){
                    clickEl = kids[j];
                    break;
                  }
                }
                try { clickEl.scrollIntoView({block:'center'}); } catch(e){}
                clickEl.click();
                return {ok:true, t: norm(clickEl.innerText||clickEl.value||c.t)};
                """
            )
            if clicked and clicked.get("ok"):
                logger.info("Clicou data escrita Início: %s", clicked.get("t"))
                return True
        except Exception as e:
            logger.warning("click data escrita: %s", e)

        # Selenium fallback
        for xp in [
            "//*[contains(.,'Início') or contains(.,'Inicio') or contains(.,'Start')]"
            "//*[contains(text(),'/')]",
            "//*[contains(text(),'Início') or contains(text(),'Inicio')]",
            "//span[contains(text(),'/') and contains(text(),':')]",
        ]:
            for el in d.find_elements(By.XPATH, xp):
                try:
                    t = (el.text or "").strip()
                    if not re.search(r"\d{2}/\d{2}/\d{4}", t):
                        continue
                    if len(t) > 80:
                        continue
                    if el.is_displayed():
                        self._click(el)
                        logger.info("Clicou data escrita (selenium): %s", t[:40])
                        return True
                except Exception:
                    continue
        return False

    def _calendar_visible(self) -> bool:
        d = self._d()
        try:
            return bool(
                d.execute_script(
                    """
                    function norm(s){ return (s||'').toLowerCase(); }
                    var months = 'janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro january february march april may june july august september october november december';
                    var nodes = document.querySelectorAll('div,table,section');
                    for (var i=0;i<nodes.length;i++){
                      var el = nodes[i];
                      var r = el.getBoundingClientRect();
                      if (r.width < 180 || r.height < 120 || r.top > 600) continue;
                      var t = norm(el.innerText||'');
                      if (t.length > 1200) continue;
                      var hasMonth = false;
                      months.split(' ').forEach(function(m){
                        if (m && t.indexOf(m) >= 0) hasMonth = true;
                      });
                      var hasDays = /\\b(1[0-9]|2[0-9]|3[01]|[1-9])\\b/.test(t)
                        && /seg|ter|qua|qui|sex|sáb|sab|dom|sun|mon|tue|wed|thu|fri|sat/i.test(t);
                      if (hasMonth && hasDays) return true;
                      // classes típicas de datepicker
                      var cls = (el.className||'').toLowerCase();
                      if (/daterangepicker|datepicker|calendar|air-datepicker|flatpickr|p-datepicker/i.test(cls)
                          && r.width > 150) return true;
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def _select_range_on_calendar(self, data_ini: date, data_fim: date) -> bool:
        """
        No calendário (2 meses): clica dia início e dia fim (range).
        Equivale a arrastar da primeira data até a selecionada.
        """
        d = self._d()
        ini_d, ini_m, ini_y = data_ini.day, data_ini.month, data_ini.year
        fim_d, fim_m, fim_y = data_fim.day, data_fim.month, data_fim.year
        mes_ini_pt = self._MES_PT[ini_m - 1]
        mes_fim_pt = self._MES_PT[fim_m - 1]
        mes_ini_en = self._MES_EN[ini_m - 1]
        mes_fim_en = self._MES_EN[fim_m - 1]

        # navega meses se preciso e clica os dias
        try:
            result = d.execute_script(
                """
                var iniDay = arguments[0], iniMonth = arguments[1], iniYear = arguments[2];
                var fimDay = arguments[3], fimMonth = arguments[4], fimYear = arguments[5];
                var mesIniPt = arguments[6], mesFimPt = arguments[7];
                var mesIniEn = arguments[8], mesFimEn = arguments[9];
                function norm(s){ return (s||'').replace(/\\s+/g,' ').trim().toLowerCase(); }
                function visible(el){
                  var r = el.getBoundingClientRect();
                  return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight + 50;
                }
                function findCalendarRoot(){
                  var sels = [
                    '.daterangepicker', '.datepicker', '.flatpickr-calendar',
                    '.air-datepicker', '.p-datepicker', '[class*="datepicker"]',
                    '[class*="daterange"]', '[class*="calendar"]'
                  ];
                  for (var s=0;s<sels.length;s++){
                    var els = document.querySelectorAll(sels[s]);
                    for (var i=0;i<els.length;i++){
                      if (visible(els[i]) && els[i].getBoundingClientRect().width > 150)
                        return els[i];
                    }
                  }
                  // fallback: div com 2 meses
                  var all = document.querySelectorAll('div');
                  var best = null, bestW = 0;
                  for (var i=0;i<all.length;i++){
                    var el = all[i];
                    if (!visible(el)) continue;
                    var r = el.getBoundingClientRect();
                    if (r.width < 280 || r.height < 150 || r.width > 900) continue;
                    var t = norm(el.innerText);
                    if (t.length > 900) continue;
                    var months = 0;
                    ['janeiro','fevereiro','março','abril','maio','junho','julho','agosto',
                     'setembro','outubro','novembro','dezembro',
                     'january','february','march','april','may','june','july','august',
                     'september','october','november','december'].forEach(function(m){
                      if (t.indexOf(m) >= 0) months++;
                    });
                    if (months >= 1 && /\\b1[0-9]|2[0-9]|3[01]|[1-9]\\b/.test(t)){
                      if (r.width > bestW){ bestW = r.width; best = el; }
                    }
                  }
                  return best;
                }
                function monthNameToNum(name){
                  var pt = ['janeiro','fevereiro','março','abril','maio','junho','julho','agosto','setembro','outubro','novembro','dezembro'];
                  var en = ['january','february','march','april','may','june','july','august','september','october','november','december'];
                  name = norm(name);
                  for (var i=0;i<12;i++){
                    if (name.indexOf(pt[i]) >= 0 || name.indexOf(en[i]) >= 0) return i+1;
                  }
                  return 0;
                }
                function clickNav(root, dir){
                  // dir: -1 prev, +1 next
                  var arrows = root.querySelectorAll(
                    'th.prev, th.next, .prev, .next, button, a, span, i, svg'
                  );
                  for (var i=0;i<arrows.length;i++){
                    var el = arrows[i];
                    if (!visible(el)) continue;
                    var cls = ((el.className&&el.className.toString)||'') + ' ' + (el.getAttribute('class')||'');
                    var aria = (el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('title')||'');
                    var t = norm(el.innerText||'');
                    var isPrev = /prev|anterior|left|←|</i.test(cls+' '+aria+' '+t);
                    var isNext = /next|próximo|proximo|right|→|>/i.test(cls+' '+aria+' '+t);
                    if (dir < 0 && isPrev){ el.click(); return true; }
                    if (dir > 0 && isNext){ el.click(); return true; }
                  }
                  // setas no canto (← →) sem classe
                  var tops = root.querySelectorAll('button,a,span,th');
                  for (var i=0;i<tops.length;i++){
                    var el = tops[i];
                    var t = (el.innerText||'').trim();
                    if (dir < 0 && (t === '←' || t === '<' || t === '‹')){ el.click(); return true; }
                    if (dir > 0 && (t === '→' || t === '>' || t === '›')){ el.click(); return true; }
                  }
                  return false;
                }
                function panelMonths(root){
                  var text = norm(root.innerText).slice(0, 400);
                  var found = [];
                  var re = /(janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro|january|february|march|april|may|june|july|august|september|october|november|december)\\s*(\\d{4})?/gi;
                  var m;
                  while ((m = re.exec(text)) && found.length < 4){
                    found.push({month: monthNameToNum(m[1]), year: m[2] ? parseInt(m[2],10) : 0});
                  }
                  return found;
                }
                function ensureMonthVisible(root, month, year){
                  for (var attempt=0; attempt<14; attempt++){
                    var panels = panelMonths(root);
                    for (var i=0;i<panels.length;i++){
                      var p = panels[i];
                      if (p.month === month && (!p.year || p.year === year || year === 0))
                        return true;
                    }
                    // decide direção
                    var first = panels[0];
                    var goNext = true;
                    if (first && first.month){
                      var cur = first.year * 12 + first.month;
                      var want = year * 12 + month;
                      goNext = want > cur;
                    }
                    if (!clickNav(root, goNext ? 1 : -1)) return false;
                  }
                  return false;
                }
                function isOffDay(td){
                  var cls = ((td.className&&td.className.toString)||'').toLowerCase();
                  if (/off|disabled|old|new|muted|outside|prev-month|next-month|other/i.test(cls))
                    return true;
                  var aria = (td.getAttribute('aria-disabled')||'');
                  if (aria === 'true') return true;
                  var st = (td.getAttribute('style')||'');
                  if (/opacity:\\s*0\\.[0-4]/i.test(st)) return true;
                  // texto cinza: cor fraca — difícil; usa class
                  return false;
                }
                function clickDay(root, day, month, year){
                  ensureMonthVisible(root, month, year);
                  // células td/span com o número do dia
                  var cells = root.querySelectorAll(
                    'td, span, div, button, a'
                  );
                  var matches = [];
                  for (var i=0;i<cells.length;i++){
                    var el = cells[i];
                    if (!visible(el)) continue;
                    if (isOffDay(el)) continue;
                    var t = (el.innerText||el.textContent||'').replace(/\\s+/g,'').trim();
                    if (t !== String(day)) continue;
                    // evita cabeçalho de dia da semana
                    if (el.tagName === 'TH') continue;
                    var r = el.getBoundingClientRect();
                    if (r.width < 12 || r.height < 12 || r.width > 80) continue;
                    // tem que estar em painel do mês certo se possível
                    var par = el, ctx = '';
                    for (var k=0;k<6 && par;k++){
                      ctx += ' ' + norm(par.innerText||'').slice(0,80);
                      par = par.parentElement;
                    }
                    var monOk = true;
                    var mnamePt = ['','janeiro','fevereiro','março','abril','maio','junho','julho','agosto','setembro','outubro','novembro','dezembro'][month];
                    var mnameEn = ['','january','february','march','april','may','june','july','august','september','october','november','december'][month];
                    // se o contexto do painel cita outro mês e não o nosso, pular
                    // (cells em painel correto preferidos)
                    var score = r.top + r.left * 0.001;
                    if (ctx.indexOf(mnamePt) >= 0 || ctx.indexOf(mnameEn) >= 0) score -= 1000;
                    matches.push({el:el, score:score});
                  }
                  matches.sort(function(a,b){ return a.score - b.score; });
                  if (!matches.length) return false;
                  matches[0].el.click();
                  return true;
                }

                var root = findCalendarRoot();
                if (!root) return {ok:false, reason:'sem_calendario'};

                var ok1 = clickDay(root, iniDay, iniMonth, iniYear);
                if (!ok1) return {ok:false, reason:'sem_dia_ini', day:iniDay};

                // pequeno delay via reflow
                var ok2 = true;
                if (fimDay !== iniDay || fimMonth !== iniMonth || fimYear !== iniYear){
                  ok2 = clickDay(root, fimDay, fimMonth, fimYear);
                } else {
                  // mesmo dia: clica de novo no mesmo (range 1 dia)
                  ok2 = clickDay(root, fimDay, fimMonth, fimYear);
                }
                return {ok: !!(ok1 && ok2), ok1:ok1, ok2:ok2};
                """,
                ini_d,
                ini_m,
                ini_y,
                fim_d,
                fim_m,
                fim_y,
                mes_ini_pt,
                mes_fim_pt,
                mes_ini_en,
                mes_fim_en,
            )
            logger.info("Calendário range: %s", result)
            if result and result.get("ok"):
                return True
        except Exception as e:
            logger.warning("select range calendar JS: %s", e)

        # Fallback Selenium: clica números do dia
        try:
            return self._select_range_selenium_days(data_ini, data_fim)
        except Exception as e:
            logger.warning("select range selenium: %s", e)
            return False

    def _select_range_selenium_days(self, data_ini: date, data_fim: date) -> bool:
        """Clica dias no calendário via Selenium + arrasta se possível."""
        d = self._d()

        def find_day_el(day: int, month: int):
            mes_pt = self._MES_PT[month - 1]
            mes_en = self._MES_EN[month - 1]
            candidates = []
            for el in d.find_elements(
                By.XPATH,
                f"//td[normalize-space()='{day}'] | //span[normalize-space()='{day}'] | "
                f"//div[normalize-space()='{day}'] | //button[normalize-space()='{day}']",
            ):
                try:
                    if not el.is_displayed():
                        continue
                    cls = (el.get_attribute("class") or "").lower()
                    if any(
                        x in cls
                        for x in (
                            "off",
                            "disabled",
                            "old",
                            "new",
                            "muted",
                            "outside",
                        )
                    ):
                        continue
                    # contexto do mês
                    try:
                        ctx = (
                            el.find_element(
                                By.XPATH, "./ancestor::div[1]"
                            ).text
                            or ""
                        ).lower()
                    except Exception:
                        ctx = ""
                    score = 0
                    if mes_pt in ctx or mes_en in ctx:
                        score -= 10
                    candidates.append((score, el))
                except Exception:
                    continue
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1] if candidates else None

        el_ini = find_day_el(data_ini.day, data_ini.month)
        if not el_ini:
            return False
        el_fim = find_day_el(data_fim.day, data_fim.month) or el_ini

        try:
            # tenta arrastar (mousedown → move → mouseup)
            ActionChains(d).click_and_hold(el_ini).pause(0.15).move_to_element(
                el_fim
            ).pause(0.1).release().perform()
            logger.info(
                "Arrastou calendário %s → %s",
                data_ini,
                data_fim,
            )
            return True
        except Exception:
            try:
                self._click(el_ini)
                self._sleep(0.25)
                if el_fim is not el_ini:
                    self._click(el_fim)
                return True
            except Exception:
                return False

    def set_date_filter(
        self,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
    ) -> None:
        """
        Fluxo Sitrax (tela real):
          1) Clica chip Data/Date
          2) Clica a data escrita (Início: 11/07/…) → abre calendário
          3) Clica/arrasta do dia início ao dia fim no calendário
          4) Clica Filtrar do popup para aplicar
        """
        d = self._d()
        data_ini = data_ini or date.today()
        data_fim = data_fim or data_ini
        if data_fim < data_ini:
            data_ini, data_fim = data_fim, data_ini
        ini_br = data_ini.strftime("%d/%m/%Y")
        fim_br = data_fim.strftime("%d/%m/%Y")

        self._close_date_popup_if_open()

        if self._date_chip_matches(data_ini, data_fim):
            logger.info("Data já no chip: %s → %s", ini_br, fim_br)
            self._trace("data_ja_ok", f"{ini_br} → {fim_br}", shot=False)
            return

        for attempt in range(1, 4):
            self._close_date_popup_if_open()
            self._sleep(0.25)

            # 1) chip Data
            if not self._click_date_chip():
                logger.warning("Chip Data/Date sumiu (tentativa %s)", attempt)
                self._save_debug(f"data_chip_sumiu_{attempt}")
                # tenta de novo com scroll no topo
                try:
                    d.execute_script("window.scrollTo(0,0);")
                except Exception:
                    pass
                self._sleep(0.3)
                if not self._click_date_chip():
                    continue

            self._sleep(0.7)
            if not self._wait_filtro_data_popup(4.0):
                logger.warning("Popup Filtro Data não abriu (tentativa %s)", attempt)
            self._save_debug(f"data_apos_chip_{attempt}")

            # 2) Clica a data escrita do Início (ex.: 11/07/2026 00:00:00) → calendário
            #    (NÃO clicar Filtrar antes — fecha o popup sem mudar a data)
            if not self._click_written_date_inicio():
                logger.warning(
                    "Não achou data escrita Início (tentativa %s)", attempt
                )
                try:
                    d.execute_script(
                        """
                        var nodes = document.querySelectorAll('span,a,div,input,b,em');
                        for (var i=0;i<nodes.length;i++){
                          var t = (nodes[i].innerText||nodes[i].value||'').replace(/\\s+/g,' ').trim();
                          if (/^\\d{2}\\/\\d{2}\\/\\d{4}/.test(t) && t.length < 28){
                            var r = nodes[i].getBoundingClientRect();
                            if (r.top > 60 && r.top < 450 && r.width > 20){
                              nodes[i].click(); return t;
                            }
                          }
                        }
                        return null;
                        """
                    )
                except Exception:
                    pass
            self._sleep(0.65)

            # espera calendário (julho/agosto com dias)
            for _ in range(14):
                if self._calendar_visible():
                    break
                # se ainda não abriu, clica de novo na data escrita
                if _ == 5:
                    self._click_written_date_inicio()
                self._sleep(0.25)
            self._save_debug(f"data_calendario_{attempt}")

            # 3) no calendário: clica dia início e arrasta/clica até o fim
            if self._calendar_visible():
                ok_cal = self._select_range_on_calendar(data_ini, data_fim)
                logger.info(
                    "Calendário select %s→%s: %s", ini_br, fim_br, ok_cal
                )
                self._sleep(0.45)
                self._save_debug(f"data_apos_calendario_{attempt}")
            else:
                logger.warning("Calendário não abriu (tentativa %s)", attempt)

            # 4) Filtrar do popup (aplica o período escolhido)
            self._click_popup_filtrar()
            self._wait_loader_gone(25)
            self._sleep(0.5)
            self._close_date_popup_if_open()
            self._sleep(0.35)

            chip_now = self._read_date_chip_text()
            self._trace(
                f"data_apos_fill_{attempt}",
                f"pedido {ini_br}→{fim_br} | chip={chip_now!r}",
                shot=True,
            )
            if self._date_chip_matches(data_ini, data_fim):
                logger.info("Data filtro OK: %s → %s", ini_br, fim_br)
                return
            logger.warning(
                "Data ainda errada (tentativa %s): chip=%r queria %s→%s",
                attempt,
                chip_now,
                ini_br,
                fim_br,
            )

        logger.warning(
            "Data filtro incompleta: chip=%r (pedido %s → %s)",
            self._read_date_chip_text(),
            ini_br,
            fim_br,
        )
        self._save_debug("data_filtro_falhou")

    def click_filtrar(self) -> None:
        """Clica no botão laranja Filtrar/Filter da barra principal (não do modal)."""
        d = self._d()
        self._close_date_popup_if_open()
        # Garante modal de veículo fechado — senão o Filter some e estoura timeout
        try:
            body0 = (d.find_element(By.TAG_NAME, "body").text or "")
            if (
                "Selecione Veículo" in body0
                or "Selecione Veiculo" in body0
                or "Select Vehicle" in body0
                or "formModalSearchVeiculo" in (d.page_source or "")[:5000]
            ):
                d.execute_script(
                    "if (typeof hideModalSearchVeiculo === 'function') hideModalSearchVeiculo();"
                    "if (typeof selectVeiculoSearch === 'function') { /* noop */ }"
                )
                self._sleep(0.4)
        except Exception:
            pass

        btn = None
        for el in d.find_elements(
            By.XPATH,
            "//button[contains(normalize-space(.),'Filtrar') or contains(normalize-space(.),'Filter')] | "
            "//a[contains(normalize-space(.),'Filtrar') or contains(normalize-space(.),'Filter')]",
        ):
            try:
                if not el.is_displayed():
                    continue
                # ignora botões dentro de modal de veículo / filtro de data
                try:
                    in_modal = el.find_elements(
                        By.XPATH,
                        "./ancestor::*[contains(@id,'Modal') or contains(@id,'modal') "
                        "or contains(@class,'modal') or contains(@class,'popup')][1]",
                    )
                    if in_modal:
                        mid = (
                            (in_modal[0].get_attribute("id") or "")
                            + " "
                            + (in_modal[0].get_attribute("class") or "")
                        ).lower()
                        if any(
                            x in mid
                            for x in (
                                "veiculo",
                                "vehicle",
                                "searchveiculo",
                                "filtrodata",
                            )
                        ):
                            continue
                except Exception:
                    pass
                parent_txt = ""
                try:
                    parent_txt = el.find_element(
                        By.XPATH,
                        "./ancestor::*[contains(@class,'modal') or contains(@class,'popup') or contains(@class,'dropdown')][1]",
                    ).text
                except Exception:
                    parent_txt = ""
                if "Filtro Data" in parent_txt or "Date Filter" in parent_txt:
                    continue
                if "Selecione Veículo" in parent_txt or "Select Vehicle" in parent_txt:
                    continue
                btn = el
                cls = el.get_attribute("class") or ""
                if (
                    "orange" in cls.lower()
                    or "primary" in cls.lower()
                    or "filtrar" in cls.lower()
                    or "filter" in cls.lower()
                ):
                    break
            except Exception:
                continue

        if not btn:
            # JS: botão Filter/Filtrar visível fora de modal de veículo
            try:
                btn = d.execute_script(
                    """
                    function vis(el) {
                      var r = el.getBoundingClientRect();
                      return r.width > 2 && r.height > 2;
                    }
                    var nodes = document.querySelectorAll('button,a,input[type=button],input[type=submit]');
                    var best = null;
                    for (var i = 0; i < nodes.length; i++) {
                      var el = nodes[i];
                      if (!vis(el)) continue;
                      var t = (el.innerText || el.value || el.textContent || '').replace(/\\s+/g,' ').trim();
                      if (t !== 'Filter' && t !== 'Filtrar' && t !== 'FILTER' && t !== 'FILTRAR') {
                        if (!/^(Filter|Filtrar)\\b/i.test(t) || t.length > 18) continue;
                      }
                      if (el.closest && (
                          el.closest('#formModalSearchVeiculo') ||
                          el.closest('[id*="ModalSearchVeiculo"]') ||
                          el.closest('[id*="FiltroData"]')
                      )) continue;
                      var cls = (el.className || '') + '';
                      if (/orange|primary|btn-warning|btn-filter/i.test(cls)) return el;
                      if (!best) best = el;
                    }
                    return best;
                    """
                )
            except Exception:
                btn = None

        if not btn:
            btn = self._find_first(
                [
                    (By.XPATH, "//button[normalize-space()='Filter' or normalize-space()='Filtrar']"),
                    (By.XPATH, "//a[normalize-space()='Filter' or normalize-space()='Filtrar']"),
                    (By.XPATH, "//button[contains(., 'Filtrar') or contains(., 'Filter')]"),
                    (By.XPATH, "//a[contains(., 'Filtrar') or contains(., 'Filter')]"),
                ],
                timeout=12,
            )
        try:
            d.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn
            )
        except Exception:
            pass
        try:
            d.execute_script("arguments[0].click();", btn)
        except Exception:
            self._click(btn)
        self._wait_loader_gone(40)
        self._sleep(1.5)

    def scrape_positions_table(self) -> list[dict]:
        """
        Lê a grade de posições (EN/PT).

        Sitrax/DataTables costuma separar:
          - tabela do thead (scrollHead)
          - tabela do tbody (scrollBody)
        O scrape antigo pegava a tabela com mais tr SEM o thead → colunas
        viravam ícones vazios e o robô gravava 0 pts com a tela CHEIA de dados.
        """
        d = self._d()
        self._sleep(0.6)
        # rola vertical + horizontal (endereço fica à direita e some do scrape)
        try:
            for _ in range(5):
                d.execute_script(
                    """
                    var boxes = document.querySelectorAll(
                      '.dataTables_scrollBody, .ui-datatable-scrollable-body, ' +
                      '.table-responsive, [class*="scroll"], .dataTables_wrapper'
                    );
                    for (var i = 0; i < boxes.length; i++) {
                      try {
                        boxes[i].scrollTop = boxes[i].scrollHeight;
                        boxes[i].scrollLeft = boxes[i].scrollWidth;
                      } catch(e) {}
                    }
                    window.scrollBy(0, 400);
                    """
                )
                self._sleep(0.2)
            # volta um pouco à esquerda para datas + modo também estarem no layout
            d.execute_script(
                """
                var boxes = document.querySelectorAll('.dataTables_scrollBody');
                for (var i = 0; i < boxes.length; i++) {
                  try { boxes[i].scrollLeft = 0; } catch(e) {}
                }
                """
            )
            self._sleep(0.15)
        except Exception:
            pass

        try:
            raw_rows = d.execute_script(
                """
                function txt(el) {
                  if (!el) return '';
                  // textContent pega texto de células cortadas/overflow
                  return (el.textContent || el.innerText || '').replace(/\\s+/g,' ').trim();
                }
                var DATE_RE = /\\d{2}\\/\\d{2}\\/\\d{4}\\s+\\d{2}:\\d{2}(:\\d{2})?/;
                var MODE_RE = /^(parked|estacionado|normal|in\\s*motion|em\\s*movimento)$/i;
                var MODE_SOFT = /parked|estacionado|normal|in\\s*motion|em\\s*movimento|movimento|igni|alerta/i;

                // ---- headers: junta thead de TODAS as tabelas (scrollHead) ----
                var headers = [];
                document.querySelectorAll('table thead th, .dataTables_scrollHead th').forEach(function(th) {
                  var t = txt(th);
                  if (t) headers.push(t);
                });
                if (!headers.length) {
                  document.querySelectorAll('table tr th').forEach(function(th) {
                    var t = txt(th);
                    if (t) headers.push(t);
                  });
                }

                function findCol(names) {
                  for (var i = 0; i < headers.length; i++) {
                    var hl = (headers[i] || '').toLowerCase();
                    for (var n = 0; n < names.length; n++) {
                      if (hl.indexOf(names[n].toLowerCase()) >= 0) return i;
                    }
                  }
                  return -1;
                }
                // NÃO usar 'Event' como endereço (coluna errada)
                var iGps = findCol(['GPS Date','Data GPS']);
                var iSis = findCol(['Date System','System Date','Data Sistema']);
                var iModo = findCol(['Mode','Modo']);
                var iEnd = findCol(['Address','Location','Endereço','Endereco']);
                var iRef = findCol(['Reference','Referência','Referencia']);
                var iTemp = findCol(['Temperature','Temperatura']);

                // ---- linhas: SÓ scrollBody (evita dobrar contagem com outra table) ----
                var trs = [];
                var bodyBoxes = document.querySelectorAll(
                  '.dataTables_scrollBody tbody tr'
                );
                if (bodyBoxes && bodyBoxes.length) {
                  trs = Array.prototype.slice.call(bodyBoxes);
                } else {
                  // fallback: a table com mais tr que tenham data
                  var best = [];
                  document.querySelectorAll('table').forEach(function(tb) {
                    var rows = tb.querySelectorAll('tbody tr');
                    var good = [];
                    rows.forEach(function(tr) {
                      if (DATE_RE.test(txt(tr))) good.push(tr);
                    });
                    if (good.length > best.length) best = good;
                  });
                  trs = best;
                }

                var out = [];
                var seen = {};
                for (var r = 0; r < trs.length; r++) {
                  var cells = trs[r].querySelectorAll('td');
                  if (!cells.length) continue;
                  var texts = [];
                  for (var c = 0; c < cells.length; c++) texts.push(txt(cells[c]));

                  function cell(idx) {
                    if (idx < 0 || idx >= texts.length) return '';
                    return texts[idx];
                  }

                  var dates = [];
                  for (var di = 0; di < texts.length; di++) {
                    var dm = texts[di].match(DATE_RE);
                    if (dm) dates.push(dm[0]);
                  }
                  var modo = cell(iModo);
                  if (!MODE_SOFT.test(modo)) {
                    modo = '';
                    for (var k = 0; k < texts.length; k++) {
                      if (MODE_RE.test(texts[k].trim()) ||
                          (MODE_SOFT.test(texts[k]) && texts[k].length < 40)) {
                        modo = texts[k].trim(); break;
                      }
                    }
                  }
                  var data_gps = cell(iGps);
                  if (!DATE_RE.test(data_gps) && dates.length) data_gps = dates[0];
                  var data_sis = cell(iSis);
                  if (!DATE_RE.test(data_sis) && dates.length > 1) data_sis = dates[1];

                  var endereco = cell(iEnd);
                  if (!endereco || endereco.length < 8) {
                    for (var k3 = 0; k3 < texts.length; k3++) {
                      var tk = texts[k3];
                      if (/rua|avenida|av\\.|rodovia|estrada|street|road/i.test(tk) ||
                          (/-\\s*[A-Za-zÀ-ú]{3,}/.test(tk) && tk.length > 12) ||
                          /metros\\s+de/i.test(tk)) {
                        endereco = tk; break;
                      }
                    }
                  }
                  var referencia = cell(iRef);
                  if (!referencia) {
                    for (var k4 = 0; k4 < texts.length; k4++) {
                      if (/metros\\s+de/i.test(texts[k4])) {
                        referencia = texts[k4]; break;
                      }
                    }
                  }

                  if (!DATE_RE.test(data_gps) && !endereco) continue;
                  // dedupe só pela data GPS (evita 16 pts a partir de 8 registros)
                  var key = (data_gps || '').replace(/\\s+/g,'');
                  if (!key) key = (dates[0] || '') + '|' + r;
                  if (seen[key]) continue;
                  seen[key] = 1;

                  out.push({
                    data_gps: data_gps || '',
                    data_sistema: data_sis || '',
                    modo: modo || '',
                    endereco: endereco || '',
                    referencia: referencia || '',
                    temperatura: cell(iTemp) || '',
                    raw_cells: texts
                  });
                }

                // fallback nuclear: varre texto da página por datas + modo
                if (!out.length) {
                  var body = (document.body && document.body.innerText) || '';
                  var lines = body.split(/\\n+/);
                  var cur = null;
                  for (var li = 0; li < lines.length; li++) {
                    var line = lines[li].replace(/\\s+/g,' ').trim();
                    if (!line) continue;
                    var dm = line.match(DATE_RE);
                    if (dm) {
                      if (cur && cur.data_gps) out.push(cur);
                      cur = {
                        data_gps: dm[0],
                        data_sistema: '',
                        modo: '',
                        endereco: '',
                        referencia: '',
                        temperatura: '',
                        raw_cells: [line]
                      };
                      var dm2 = line.match(new RegExp(DATE_RE.source, 'g'));
                      if (dm2 && dm2.length > 1) cur.data_sistema = dm2[1];
                      if (MODE_RE.test(line)) {
                        var mm = line.match(MODE_RE);
                        if (mm) cur.modo = mm[0];
                      }
                    } else if (cur) {
                      if (!cur.modo && MODE_RE.test(line) && line.length < 40) cur.modo = line;
                      if (!cur.endereco && (/\\([A-Z]{2}\\)/.test(line) || /rua|street|avenida/i.test(line)))
                        cur.endereco = line;
                    }
                  }
                  if (cur && cur.data_gps) out.push(cur);
                }
                return out;
                """
            )
            if raw_rows and isinstance(raw_rows, list):
                logger.info("Posições lidas (JS): %s", len(raw_rows))
                if raw_rows:
                    return raw_rows
        except Exception as e:
            logger.warning("Scrape JS falhou: %s", e)

        # Fallback Selenium: qualquer tr com data
        rows_data: list[dict] = []
        try:
            for row in d.find_elements(By.CSS_SELECTOR, "table tbody tr, table tr"):
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 2:
                    continue
                texts = [c.text.strip() for c in cells]
                item = {
                    "data_gps": "",
                    "data_sistema": "",
                    "modo": "",
                    "endereco": "",
                    "referencia": "",
                    "temperatura": "",
                    "raw_cells": texts,
                }
                for t in texts:
                    if re.search(r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}", t):
                        if not item["data_gps"]:
                            item["data_gps"] = t
                        elif not item["data_sistema"]:
                            item["data_sistema"] = t
                    elif re.search(
                        r"parked|estacionado|normal|in motion|movimento|igni", t, re.I
                    ):
                        item["modo"] = t
                    elif re.search(r"\([A-Z]{2}\)", t) or re.search(
                        r"rua|avenida|street", t, re.I
                    ):
                        item["endereco"] = t
                if item["data_gps"] or item["endereco"]:
                    rows_data.append(item)
        except Exception as e:
            logger.warning("Scrape selenium: %s", e)
        logger.info("Posições lidas (selenium): %s", len(rows_data))
        return rows_data

    def try_scroll_all(self) -> None:
        d = self._d()
        for _ in range(12):
            try:
                d.execute_script(
                    """
                    var boxes = document.querySelectorAll(
                      '.dataTables_scrollBody, .ui-datatable-scrollable-body, ' +
                      '.table-responsive, tbody'
                    );
                    for (var i = 0; i < boxes.length; i++) {
                      try { boxes[i].scrollTop = boxes[i].scrollHeight; } catch(e) {}
                    }
                    window.scrollBy(0, 1500);
                    """
                )
            except Exception:
                d.execute_script("window.scrollBy(0, 1200);")
            self._sleep(0.25)

    def _vehicle_modal_open(self) -> bool:
        """True se o modal Selecione Veículo / input de placa está ativo."""
        d = self._d()
        try:
            if d.execute_script(
                """
                var inp = document.querySelector(
                  "#itFiltroCveiPlaca, input[id='formModalSearchVeiculo:itCveiPlaca'], input[id*='itCveiPlaca']"
                );
                if (inp) {
                  var r = inp.getBoundingClientRect();
                  if (r.width > 2 && r.height > 2) return true;
                }
                var body = (document.body && document.body.innerText) || '';
                return body.indexOf('Selecione Veículo') >= 0
                    || body.indexOf('Selecione Veiculo') >= 0
                    || body.indexOf('Select Vehicle') >= 0;
                """
            ):
                return True
        except Exception:
            pass
        return False

    def _on_posicoes_screen(self) -> bool:
        try:
            return bool(self._posicoes_screen_ready())
        except Exception:
            return False

    def clear_vehicle_chip(self) -> bool:
        """
        Clica no X do chip 'Veículo: PLACA' na barra de filtros
        (limpa o veículo atual sem sair de Posições).
        """
        d = self._d()
        self._close_date_popup_if_open()
        try:
            clicked = d.execute_script(
                """
                function visible(el) {
                  var r = el.getBoundingClientRect();
                  return r.width > 1 && r.height > 1;
                }
                // 1) botão/ícone X dentro do chip Veículo/Vehicle
                var chips = document.querySelectorAll('div,span,button,a,li,label');
                for (var i = 0; i < chips.length; i++) {
                  var el = chips[i];
                  if (!visible(el)) continue;
                  var t = (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim();
                  if (!t || t.length > 80) continue;
                  var isVeic =
                    /^Ve[ií]culo\\s*:/i.test(t) ||
                    /^Vehicle\\s*:/i.test(t) ||
                    (/Ve[ií]culo|Vehicle/i.test(t) && /[A-Z]{3}\\d/i.test(t));
                  if (!isVeic) continue;
                  var close = el.querySelector(
                    'i.fa-xmark, i.fa-times, i.fa-close, button, a, [class*="close"], [class*="Clear"], [onclick*="clear"], [onclick*="Clear"], [onclick*="remove"]'
                  );
                  if (!close) {
                    // irmãos / filhos com X
                    var kids = el.querySelectorAll('i,button,a,span');
                    for (var k = 0; k < kids.length; k++) {
                      var c = kids[k];
                      var cls = (c.className || '') + '';
                      var oc = c.getAttribute('onclick') || '';
                      var tx = (c.innerText || c.textContent || '').trim();
                      if (/fa-x|times|close|clear|remove/i.test(cls + ' ' + oc) || tx === '×' || tx === 'x' || tx === 'X') {
                        close = c; break;
                      }
                    }
                  }
                  if (close && visible(close)) {
                    close.click();
                    return 'chip-x';
                  }
                  // chip inteiro às vezes tem o X como irmão
                  var sib = el.nextElementSibling;
                  if (sib && visible(sib)) {
                    var scls = (sib.className || '') + (sib.getAttribute('onclick') || '');
                    if (/close|clear|times|xmark|remove/i.test(scls)) {
                      sib.click();
                      return 'chip-sibling-x';
                    }
                  }
                }
                // 2) qualquer X roxo/branco perto de texto de placa no filtro
                var icons = document.querySelectorAll('i.fa-xmark, i.fa-times, i.fa-close, a.swTopBarIconCloseLight');
                for (var j = 0; j < icons.length; j++) {
                  var ic = icons[j];
                  if (!visible(ic)) continue;
                  var parent = ic.closest('div,span,button,a,li') || ic.parentElement;
                  var pt = ((parent && (parent.innerText || parent.textContent)) || '');
                  if (/Ve[ií]culo|Vehicle|placa|plate/i.test(pt) || /[A-Z]{3}\\d[A-Z0-9]\\d{2}/i.test(pt)) {
                    ic.click();
                    return 'icon-near-plate';
                  }
                }
                return null;
                """
            )
            if clicked:
                logger.info("Limpou chip Veículo (%s)", clicked)
                self._wait_loader_gone(20)
                self._sleep(0.5)
                return True
        except Exception as e:
            logger.warning("clear_vehicle_chip JS: %s", e)
        return False

    def prepare_historico_warm(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        clear_previous: bool = False,
    ) -> None:
        """
        Caminho rápido (sessão já logada):
          [X do chip] → modal Veículos → filtra placa → Selecionar → data → Filter.
        """
        self._close_date_popup_if_open()
        if clear_previous:
            self.clear_vehicle_chip()
            self._sleep(0.3)
        # Se modal já aberto (warm idle), usa; senão garante Posições + Veículo
        if not self._vehicle_modal_open():
            if not self._on_posicoes_screen():
                self.open_posicoes()
                self._sleep(0.6)
            try:
                self.open_vehicle_selector()
            except TimeoutException:
                logger.warning("Veículo não achado no warm; reabrindo Posições…")
                self.open_posicoes()
                self._sleep(0.8)
                self.open_vehicle_selector()
        self.load_vehicle_list(placa=placa)
        self.select_vehicle_by_plate(placa)
        self.set_date_filter(data_ini, data_fim)
        self.click_filtrar()
        self._sleep(1.2)

    def prepare_next_fleet_plate(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        first: bool = False,
    ) -> None:
        """
        Entre carros na frota (mesmo Chrome):
          1º: Posições + Veículo normal
          2º+: X no chip do veículo → lupa/modal → outra placa → Filter
        """
        if first:
            if not self._on_posicoes_screen():
                self.open_posicoes()
                self._sleep(0.5)
            self.prepare_historico_warm(
                placa, data_ini, data_fim, clear_previous=False
            )
        else:
            # limpa chip e escolhe o próximo sem voltar ao login
            self.prepare_historico_warm(
                placa, data_ini, data_fim, clear_previous=True
            )

    def vehicle_chip_has_plate(self, placa: str) -> bool:
        """
        True se a barra de filtros mostra o chip Veículo com essa placa.
        Aceita PT/EN, com/sem espaço após ':' (ex.: Vehicle:PDY1G26).
        """
        placa_u = self._norm_placa(placa)
        if not placa_u:
            return False
        try:
            return bool(
                self._d().execute_script(
                    """
                    var alvo = (arguments[0] || '').toUpperCase().replace(/[^A-Z0-9]/g,'');
                    if (!alvo || alvo.length < 5) return false;
                    function norm(s) {
                      return (s || '').toUpperCase().replace(/[^A-Z0-9:\\s]/g,'');
                    }
                    var body = norm(document.body && document.body.innerText);
                    if (body.indexOf(alvo) < 0) return false;
                    // VEICULO:PLACA / VEHICLE: PLACA / VEÍCULO : PLACA
                    var re = new RegExp(
                      'VE[IÍ]?CULO\\\\s*:\\\\s*' + alvo + '|' +
                      'VEHICLE\\\\s*:\\\\s*' + alvo
                    );
                    if (re.test(body)) return true;
                    // HTML cru (às vezes o chip só está no source)
                    try {
                      var src = (document.documentElement.innerHTML || '').toUpperCase();
                      if (src.indexOf(alvo) >= 0 &&
                          (src.indexOf('VEHICLE') >= 0 || src.indexOf('VEICULO') >= 0 ||
                           src.indexOf('VEÍCULO') >= 0)) {
                        if (new RegExp(alvo).test(src)) return true;
                      }
                    } catch (e) {}
                    // nós curtos na barra de filtros
                    var nodes = document.querySelectorAll(
                      'div,span,button,a,label,li,p,b,strong'
                    );
                    for (var i = 0; i < Math.min(nodes.length, 400); i++) {
                      var raw = (nodes[i].textContent || nodes[i].innerText || '');
                      var t = raw.replace(/\\s+/g,' ').trim().toUpperCase();
                      if (!t || t.length > 80) continue;
                      var tn = t.replace(/[^A-Z0-9:]/g,'');
                      if (tn.indexOf(alvo) < 0) continue;
                      if (/VEICULO|VEHICLE|VEÍCULO/.test(t) || tn.indexOf('VEHICLE'+alvo) >= 0
                          || tn.indexOf('VEICULO'+alvo) >= 0) {
                        return true;
                      }
                      // só a placa num chip roxo/filtro
                      if (tn === alvo || tn.indexOf(':'+alvo) >= 0) return true;
                    }
                    return false;
                    """,
                    placa_u,
                )
            )
        except Exception:
            return False

    def wait_vehicle_chip(self, placa: str, timeout: float = 8.0) -> bool:
        """Espera o chip estabilizar (evita chip_sem por página ainda carregando)."""
        end = time.time() + timeout
        while time.time() < end:
            if self.vehicle_chip_has_plate(placa):
                return True
            self._sleep(0.45)
        return self.vehicle_chip_has_plate(placa)

    def sitrax_says_no_records(self) -> bool:
        """
        True quando o Sitrax confirma busca sem dados (caso real, não bug).
        Ex.: "Não foram encontrados registros" / "Showing: 0 Register(s)".
        """
        try:
            return bool(
                self._d().execute_script(
                    """
                    var body = ((document.body && document.body.innerText) || '');
                    if (/n[aã]o\\s+foram\\s+encontrados\\s+registros/i.test(body)) return true;
                    if (/no\\s+records?\\s+(were\\s+)?found/i.test(body)) return true;
                    if (/nenhum\\s+registro/i.test(body)) return true;
                    if (/mostrando\\s*:\\s*0\\s*registro/i.test(body)) return true;
                    if (/showing\\s*:\\s*0\\s*register/i.test(body)) return true;
                    var alerts = document.querySelectorAll(
                      '.alert, .toast, [class*="alert"], [class*="warning"], [role="alert"]'
                    );
                    for (var i = 0; i < alerts.length; i++) {
                      var t = (alerts[i].innerText || alerts[i].textContent || '');
                      if (/n[aã]o\\s+foram\\s+encontrados|no\\s+records/i.test(t)) return true;
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def showing_zero_records(self) -> bool:
        """True se a grade mostra explicitamente 0 registros (PT/EN)."""
        return self.count_sitrax_registers() == 0 and (
            self.sitrax_says_no_records()
            or bool(
                self._d().execute_script(
                    """
                    var body = ((document.body && document.body.innerText) || '');
                    return /mostrando\\s*:\\s*0\\b/i.test(body)
                        || /showing\\s*:\\s*0\\b/i.test(body);
                    """
                )
            )
        )

    def count_sitrax_registers(self) -> int:
        """
        Lê o contador do rodapé Sitrax: 'Mostrando: 510 Registro(s)' / 'Showing: 510'.
        -1 se não achar o número.
        """
        try:
            n = self._d().execute_script(
                """
                var body = ((document.body && document.body.innerText) || '');
                var m = body.match(/Mostrando\\s*:\\s*(\\d+)/i)
                     || body.match(/Showing\\s*:\\s*(\\d+)/i)
                     || body.match(/(\\d+)\\s*Registro\\(s\\)/i)
                     || body.match(/(\\d+)\\s*Register\\(s\\)/i);
                if (m) return parseInt(m[1], 10);
                return -1;
                """
            )
            return int(n) if n is not None else -1
        except Exception:
            return -1

    def grid_has_data_rows(self) -> bool:
        """True se há linhas com data GPS na grade (scrape falhou mas tem dado)."""
        try:
            n = self._d().execute_script(
                """
                var DATE_RE = /\\d{2}\\/\\d{2}\\/\\d{4}\\s+\\d{2}:\\d{2}/;
                var trs = document.querySelectorAll(
                  '.dataTables_scrollBody tbody tr, table tbody tr'
                );
                var c = 0;
                for (var i = 0; i < trs.length; i++) {
                  var t = trs[i].innerText || trs[i].textContent || '';
                  if (DATE_RE.test(t)) c++;
                }
                return c;
                """
            )
            return int(n or 0) > 0
        except Exception:
            return False

    def wait_positions_grid(self, timeout: float = 30) -> int:
        """
        Espera a grade de posições (PT/EN).
        Conta linhas com data GPS (não só tr vazios do DataTables).
        Retorna 0 cedo se o Sitrax declarar explicitamente sem registros.
        """
        d = self._d()
        end = time.time() + timeout
        last_n = 0
        empty_confirmed_since: Optional[float] = None
        while time.time() < end:
            try:
                if self.sitrax_says_no_records():
                    if empty_confirmed_since is None:
                        empty_confirmed_since = time.time()
                    # confirma 0 real após ~1.2s (toast + "Mostrando: 0")
                    if time.time() - empty_confirmed_since >= 1.2:
                        return 0
                else:
                    empty_confirmed_since = None

                n = d.execute_script(
                    """
                    var body = (document.body && document.body.innerText) || '';
                    var m = body.match(/Mostrando\\s*:\\s*(\\d+)/i)
                         || body.match(/Showing\\s*:\\s*(\\d+)/i)
                         || body.match(/(\\d+)\\s*Registro/i)
                         || body.match(/(\\d+)\\s*Record/i);
                    if (m && parseInt(m[1], 10) > 0) return parseInt(m[1], 10);

                    var DATE_RE = /\\d{2}\\/\\d{2}\\/\\d{4}\\s+\\d{2}:\\d{2}/;
                    var trs = document.querySelectorAll(
                      '.dataTables_scrollBody tbody tr, table tbody tr, table tr'
                    );
                    var count = 0;
                    for (var i = 0; i < trs.length; i++) {
                      var t = (trs[i].innerText || trs[i].textContent || '');
                      if (DATE_RE.test(t)) count++;
                    }
                    return count;
                    """
                )
                last_n = int(n or 0)
                if last_n > 0:
                    return last_n
            except Exception:
                pass
            self._sleep(0.45)
        return last_n

    def fetch_positions_for_fleet_plate(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        clear_previous: bool = False,
    ) -> list:
        """
        Fluxo robusto 1 placa na frota:
          limpa chip → escolhe placa → Filter → espera grade → scrape.

        0 posições pode ser:
          - REAL: Sitrax "não foram encontrados registros" → aceita na 1ª confirmação
          - BUG: chip/filter errado → tenta de novo (até 3x)
        """
        placa_u = self._norm_placa(placa)
        last_rows: list = []
        # Até 2 tentativas: 2ª só se NÃO for "Mostrando: 0" (zero real = fim)
        for attempt in range(2):
            try:
                self.prepare_historico_warm(
                    placa_u,
                    data_ini,
                    data_fim,
                    clear_previous=(clear_previous or attempt > 0),
                )
            except Exception as e:
                logger.warning(
                    "prepare frota %s tentativa %s: %s", placa_u, attempt + 1, e
                )
                if attempt == 0:
                    try:
                        self.open_posicoes()
                        self._sleep(1)
                    except Exception:
                        pass
                continue

            try:
                self._d().execute_script(
                    "if (typeof hideModalSearchVeiculo === 'function') "
                    "hideModalSearchVeiculo();"
                )
            except Exception:
                pass
            self._sleep(0.5)

            # Espera o chip (página/AJAX); se falhar, AINDA tenta Filter
            # (caso PDY1G26: tela já tinha Vehicle:PDY1G26 + 852 regs e chip_sem era falso)
            chip_ok = self.wait_vehicle_chip(placa_u, timeout=7.0)
            if not chip_ok:
                logger.warning(
                    "Chip %s não confirmado após espera (tentativa %s) — segue Filter",
                    placa_u,
                    attempt + 1,
                )
                self._trace(
                    f"chip_lento_{placa_u}",
                    f"Chip {placa_u} ainda não estável; segue Filter (pode ser timing)",
                    ok=True,
                    shot=False,
                )
                # NÃO clear_vehicle_chip aqui — pode apagar seleção boa em meio ao AJAX

            try:
                self.click_filtrar()
            except Exception as e:
                logger.warning("re-Filter %s: %s", placa_u, e)

            n_hint = self.wait_positions_grid(timeout=28)
            # se ainda 0 e SEM mensagem de vazio: Filter de novo e espera mais
            if n_hint == 0 and not (
                self.sitrax_says_no_records() or self.showing_zero_records()
            ):
                try:
                    self.click_filtrar()
                except Exception:
                    pass
                n_hint = self.wait_positions_grid(timeout=20)

            self.try_scroll_all()
            rows = self.scrape_positions_table()
            last_rows = rows or []
            n_scrape = len(last_rows)

            # Zero REAL só com confirmação explícita do Sitrax — NÃO use n_hint==0
            zero_real = self.sitrax_says_no_records() or self.showing_zero_records()
            site_n = self.count_sitrax_registers()

            logger.info(
                "Frota %s tentativa %s: chip=%s grid~%s scrape=%s site=%s zero_real=%s",
                placa_u,
                attempt + 1,
                chip_ok,
                n_hint,
                n_scrape,
                site_n,
                zero_real,
            )

            # Sucesso se leu dados — mesmo sem chip confirmado (timing)
            if n_scrape > 0 or (site_n is not None and site_n > 0 and self.grid_has_data_rows()):
                if n_scrape == 0 and self.grid_has_data_rows():
                    self.try_scroll_all()
                    last_rows = self.scrape_positions_table() or []
                    n_scrape = len(last_rows)
                if n_scrape > 0:
                    self._trace(
                        f"frota_rows_{placa_u}",
                        f"{n_scrape} linha(s) scrapadas (hint grid {n_hint}"
                        f"{', chip lento' if not chip_ok else ''})",
                        ok=True,
                    )
                    return last_rows

            # Sitrax mostrou explicitamente 0 → OK, sem retry
            if zero_real and (chip_ok or self.vehicle_chip_has_plate(placa_u)):
                self._trace(
                    f"frota_sem_dados_{placa_u}",
                    f"{placa_u}: Sitrax 0 registros no período — OK (sem retry)",
                    ok=True,
                    shot=True,
                )
                return []

            # Grade com dados mas scrape vazio → retry
            if attempt == 0 and (self.grid_has_data_rows() or n_hint > 0 or (site_n or 0) > 0):
                self._trace(
                    f"frota_scrape_miss_{placa_u}",
                    f"{placa_u}: há indício de dados (grid~{n_hint}) mas scrape 0 — retry",
                    ok=False,
                )
                continue

            # n_hint==0 sem toast de vazio: ainda pode ser lento — 1 retry
            if attempt == 0 and n_hint == 0 and not zero_real:
                self._trace(
                    f"frota_grid_lento_{placa_u}",
                    f"{placa_u}: grade ainda vazia sem msg de 0 — retry",
                    ok=False,
                )
                continue

            # chip nunca confirmou E sem dados → aí sim refaz select
            if attempt == 0 and not chip_ok:
                self._trace(
                    f"chip_sem_{placa_u}",
                    f"Chip {placa_u} não confirmado e sem dados — refaz select",
                    ok=False,
                    shot=True,
                )
                try:
                    self.clear_vehicle_chip()
                except Exception:
                    pass
                continue

            self._save_debug(
                f"frota_zero_{placa_u}_t{attempt+1}",
                f"0 posições para {placa_u} tentativa {attempt+1} "
                f"(zero_real={zero_real} n_hint={n_hint} chip={chip_ok})",
                ok=False,
            )
            break

        return last_rows

    def return_to_vehicles_ready(self) -> None:
        """
        Após uma consulta: fecha popups e deixa o modal Veículos aberto
        com a lista pronta para a próxima placa.
        """
        d = self._d()
        self._close_date_popup_if_open()
        try:
            d.execute_script(
                "if (typeof hideModalSearchVeiculo === 'function') hideModalSearchVeiculo();"
            )
        except Exception:
            pass
        try:
            from selenium.webdriver.common.keys import Keys

            d.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
        self._sleep(0.4)

        if not self._on_posicoes_screen():
            self.open_posicoes()
            self._sleep(0.5)
        self._close_date_popup_if_open()
        if not self._vehicle_modal_open():
            self.open_vehicle_selector()
        # lista cheia (sem placa) — pronta para o próximo filtro
        try:
            self.load_vehicle_list()
        except Exception:
            # se lista falhar, tenta lupa/reload 1x
            self.open_vehicle_selector()
            self.load_vehicle_list()
        n = self._count_vehicle_items()
        self._trace(
            "warm_idle_veiculos",
            f"Aguardando próxima placa — {n} veículo(s) na lista",
            ok=True,
        )
        logger.info("Sessão pronta em Veículos (%s itens)", n)

    def _prepare_historico_filtrado(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
    ) -> None:
        """Abre Posições, escolhe veículo, data e filtra (lista pronta na tela)."""
        self._sleep(2)  # dashboard JSF estabilizar (Railway)
        self.open_posicoes()
        self._sleep(1)
        self._close_date_popup_if_open()
        try:
            self.open_vehicle_selector()
        except TimeoutException:
            # se open_posicoes seguiu otimista, tenta reabrir Posições 1x
            logger.warning("Veículo não achado; reabrindo Posições…")
            self.open_posicoes()
            self._sleep(1)
            self.open_vehicle_selector()
        self.load_vehicle_list(placa=placa)
        self.select_vehicle_by_plate(placa)
        self.set_date_filter(data_ini, data_fim)
        self.click_filtrar()
        self._sleep(2)

    def _click_export_cloud_icon(self) -> None:
        """Abre o menu Export (icone de nuvem ao lado do Filter)."""
        d = self._d()
        self._sleep(0.5)
        clicked = False
        try:
            res = d.execute_script(
                """
                function visible(el) {
                  var r = el.getBoundingClientRect();
                  return r.width > 2 && r.height > 2;
                }
                var sels = [
                  'i.fa-cloud-arrow-down', 'i.fa-cloud-download', 'i.fa-cloud-download-alt',
                  'i.fa-cloud', 'i.fa-download',
                  'i[class*="cloud"]', 'i[class*="download"]',
                  'svg[class*="cloud"]',
                  '[onclick*="export"]', '[onclick*="Export"]',
                  '[onclick*="download"]', '[onclick*="Download"]',
                  '[title*="Download"]', '[title*="Export"]', '[aria-label*="Download"]'
                ];
                for (var s = 0; s < sels.length; s++) {
                  var nodes = document.querySelectorAll(sels[s]);
                  for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    if (el.closest && el.closest('#sidebar-menu')) continue;
                    if (!visible(el)) continue;
                    el.scrollIntoView({block:'center'});
                    el.click();
                    return 'sel:' + sels[s];
                  }
                }
                var filters = document.querySelectorAll('button,a');
                for (var f = 0; f < filters.length; f++) {
                  var t = (filters[f].innerText || filters[f].textContent || '').trim();
                  if (t !== 'Filter' && t !== 'Filtrar' && t.indexOf('Filter') < 0 && t.indexOf('Filtrar') < 0) continue;
                  var parent = filters[f].parentElement;
                  if (!parent) continue;
                  var kids = parent.querySelectorAll('i,button,a,span,div,svg');
                  for (var k = 0; k < kids.length; k++) {
                    var c = kids[k];
                    if (c === filters[f]) continue;
                    var cls = (c.className || '') + '';
                    var oc = c.getAttribute('onclick') || '';
                    if (/cloud|download|export/i.test(cls + ' ' + oc)) {
                      c.click();
                      return 'near-filter';
                    }
                  }
                  var sib = filters[f].nextElementSibling;
                  if (sib) { sib.click(); return 'filter-next-sibling'; }
                }
                var all = document.querySelectorAll('i,button,a,span,div');
                for (var j = 0; j < all.length; j++) {
                  var e = all[j];
                  if (e.closest && e.closest('#sidebar-menu')) continue;
                  if (!visible(e)) continue;
                  var cc = (e.className || '') + ' ' + (e.getAttribute('onclick') || '');
                  if (/fa-cloud|cloud-arrow|cloud-download|exportPdf|export.*pdf|download.*pdf/i.test(cc)) {
                    e.click();
                    return 'heuristic-cloud';
                  }
                }
                return null;
                """
            )
            if res:
                clicked = True
                logger.info("Download cloud click: %s", res)
                self._trace("download_cloud", f"Clicou nuvem: {res}")
        except Exception as e:
            logger.warning("JS download cloud: %s", e)

        if not clicked:
            for sel in [
                (
                    By.CSS_SELECTOR,
                    "i.fa-cloud-arrow-down, i.fa-cloud-download-alt, i.fa-cloud, i[class*='cloud']",
                ),
                (
                    By.CSS_SELECTOR,
                    "[onclick*='export'], [onclick*='Export'], [onclick*='download']",
                ),
            ]:
                try:
                    for el in d.find_elements(*sel):
                        try:
                            d.execute_script("arguments[0].click();", el)
                            clicked = True
                            self._trace("download_cloud", f"Fallback {sel}")
                            break
                        except Exception:
                            continue
                except Exception:
                    continue
                if clicked:
                    break

        if not clicked:
            self._save_debug(
                "download_nao_encontrado",
                "Icone de nuvem (download) nao encontrado ao lado do Filter",
                ok=False,
            )
            raise TimeoutException(
                "Nao achei o botao de download (nuvem) do historico. Abra /debug."
            )
        self._trace("download_cloud_aberta", "Nuvem clicada — esperando menu Export")
        self._sleep(0.8)

    def _activate_pdf_menu_item(self) -> bool:
        """Clica de verdade no item PDF file (sobe ate o <a>/li com handler JSF)."""
        d = self._d()
        xpaths = (
            "//a[normalize-space()='PDF file' or normalize-space()='PDF File' or normalize-space()='Arquivo PDF']",
            "//span[normalize-space()='PDF file' or normalize-space()='PDF File']/ancestor::a[1]",
            "//*[normalize-space()='PDF file']/ancestor::*[self::a or self::button or self::li][1]",
            "//a[contains(.,'PDF file') and not(contains(.,'XLS'))]",
            "//*[contains(@class,'menuitem') or contains(@class,'dropdown')]"
            "//*[normalize-space()='PDF file' or normalize-space()='Arquivo PDF']",
        )
        for xp in xpaths:
            try:
                for el in d.find_elements(By.XPATH, xp):
                    try:
                        if not el.is_displayed():
                            continue
                        t = (el.text or "").replace("\n", " ").strip()
                        if re.search(r"xls", t, re.I) and "pdf" not in t.lower():
                            continue
                        d.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", el
                        )
                        self._sleep(0.15)
                        try:
                            ActionChains(d).move_to_element(el).pause(0.05).click(
                                el
                            ).perform()
                        except Exception:
                            d.execute_script("arguments[0].click();", el)
                        d.execute_script(
                            """
                            var el = arguments[0];
                            var t = el;
                            for (var i = 0; i < 6 && t; i++) {
                              var tag = (t.tagName || '').toUpperCase();
                              var oc = t.getAttribute && (t.getAttribute('onclick') || '');
                              var href = t.getAttribute && (t.getAttribute('href') || '');
                              if (tag === 'A' || tag === 'BUTTON' || oc || href ||
                                  (t.getAttribute && t.getAttribute('role') === 'menuitem')) {
                                el = t; break;
                              }
                              t = t.parentElement;
                            }
                            el.focus && el.focus();
                            ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(type) {
                              el.dispatchEvent(new MouseEvent(type, {
                                bubbles: true, cancelable: true, view: window, buttons: 1
                              }));
                            });
                            if (typeof el.click === 'function') el.click();
                            """,
                            el,
                        )
                        logger.info("Clique PDF menu via xpath text=%s", t[:40])
                        return True
                    except Exception as e:
                        logger.debug("xpath PDF fail: %s", e)
                        continue
            except Exception:
                continue

        try:
            res = d.execute_script(
                """
                function visible(el) {
                  var r = el.getBoundingClientRect();
                  return r.width > 1 && r.height > 1;
                }
                function activate(el) {
                  var t = el;
                  for (var i = 0; i < 8 && t; i++) {
                    var tag = (t.tagName || '').toUpperCase();
                    var oc = t.getAttribute ? (t.getAttribute('onclick') || '') : '';
                    var href = t.getAttribute ? (t.getAttribute('href') || '') : '';
                    var role = t.getAttribute ? (t.getAttribute('role') || '') : '';
                    if (tag === 'A' || tag === 'BUTTON' || oc || href || role === 'menuitem'
                        || /menuitem|dropdown-item/i.test(t.className || '')) {
                      el = t; break;
                    }
                    t = t.parentElement;
                  }
                  el.scrollIntoView({block:'center'});
                  el.focus && el.focus();
                  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(type) {
                    el.dispatchEvent(new MouseEvent(type, {
                      bubbles: true, cancelable: true, view: window, buttons: 1
                    }));
                  });
                  if (typeof el.click === 'function') el.click();
                  return (el.innerText || el.textContent || 'PDF').replace(/\\s+/g,' ').trim().slice(0,40);
                }
                var nodes = document.querySelectorAll('a,button,span,div,li,label,p');
                var candidates = [];
                for (var i = 0; i < nodes.length; i++) {
                  var el = nodes[i];
                  if (!visible(el)) continue;
                  var tx = (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim();
                  if (!tx || /xls/i.test(tx)) continue;
                  if (tx === 'PDF file' || tx === 'PDF File' || tx === 'Arquivo PDF'
                      || tx === 'Export PDF' || tx === 'Exportar PDF'
                      || (/^PDF\\s*file$/i.test(tx))
                      || (tx.indexOf('PDF file') >= 0 && tx.length < 40)) {
                    candidates.push(el);
                  }
                }
                candidates.sort(function(a,b) {
                  return (a.innerText||'').trim().length - (b.innerText||'').trim().length;
                });
                if (candidates.length) return activate(candidates[0]);
                return null;
                """
            )
            if res:
                logger.info("Clique PDF menu via JS: %s", res)
                return True
        except Exception as e:
            logger.warning("JS PDF menu: %s", e)
        return False

    def _click_download_cloud(self) -> None:
        """Clica nuvem (Export) -> item PDF file. Nao espera o arquivo."""
        self._ensure_download_behavior()
        self._click_export_cloud_icon()

        pdf_clicked = False
        end_menu = time.time() + 15
        while time.time() < end_menu and not pdf_clicked:
            try:
                ready = self._d().execute_script(
                    """
                    var body = document.body.innerText || '';
                    return body.indexOf('PDF file') >= 0
                        || body.indexOf('PDF File') >= 0
                        || body.indexOf('Arquivo PDF') >= 0;
                    """
                )
            except Exception:
                ready = False
            if ready and self._activate_pdf_menu_item():
                pdf_clicked = True
                self._trace(
                    "download_pdf_file",
                    "Clicou no menu: PDF file (handler JSF)",
                    ok=True,
                    shot=True,
                )
                break
            self._sleep(0.35)

        if not pdf_clicked:
            self._save_debug(
                "download_menu_pdf_nao_clicado",
                "Menu Export aberto mas PDF file nao foi clicado",
                ok=False,
            )
            raise TimeoutException(
                "Abriu Export mas nao clicou em 'PDF file'. Abra /debug."
            )
        # Popup Sitrax EN: "Your download will start in a few seconds." + Ok
        self._dismiss_download_popup()
        self._sleep(0.8)

    def _dismiss_download_popup(self) -> None:
        """Fecha o alerta 'Your download will start in a few seconds' / Ok."""
        d = self._d()
        end = time.time() + 12
        while time.time() < end:
            try:
                clicked = d.execute_script(
                    """
                    var body = (document.body && document.body.innerText) || '';
                    var has =
                      body.indexOf('download will start') >= 0
                      || body.indexOf('download começ') >= 0
                      || body.indexOf('download comec') >= 0
                      || body.indexOf('Seu download') >= 0
                      || body.indexOf('few seconds') >= 0;
                    if (!has) return null;
                    var nodes = document.querySelectorAll('button,a,span,input');
                    for (var i = 0; i < nodes.length; i++) {
                      var el = nodes[i];
                      var t = (el.innerText || el.textContent || el.value || '')
                        .replace(/\\s+/g,' ').trim();
                      if (/^(Ok|OK|Oke|Yes|Sim)$/i.test(t)) {
                        el.click();
                        return t;
                      }
                    }
                    // botão laranja genérico no modal
                    var btns = document.querySelectorAll(
                      '.ui-dialog button, .modal button, [role="dialog"] button'
                    );
                    for (var j = 0; j < btns.length; j++) {
                      if (btns[j].offsetParent !== null) {
                        btns[j].click();
                        return 'dialog-btn';
                      }
                    }
                    return null;
                    """
                )
                if clicked:
                    logger.info("Popup download: clicou %s", clicked)
                    self._trace(
                        "download_popup_ok",
                        f"Fechou alerta de download ({clicked})",
                        shot=True,
                    )
                    self._sleep(0.5)
                    return
            except Exception as e:
                logger.debug("dismiss popup: %s", e)
            # Selenium fallback
            for xp in (
                "//button[normalize-space()='Ok' or normalize-space()='OK']",
                "//button[contains(.,'Ok') or contains(.,'OK')]",
                "//*[contains(.,'download will start')]/following::button[1]",
            ):
                try:
                    for el in d.find_elements(By.XPATH, xp):
                        if el.is_displayed():
                            d.execute_script("arguments[0].click();", el)
                            self._trace("download_popup_ok", "Ok (selenium)", shot=True)
                            return
                except Exception:
                    continue
            self._sleep(0.35)

    def _try_save_pdf_from_open_tabs(self, dest: Path) -> Optional[Path]:
        """Se o Sitrax abriu o PDF em nova aba, baixa via URL + cookies da sessao."""
        d = self._d()
        main = d.current_window_handle
        saved: Optional[Path] = None
        try:
            import urllib.request
            import http.cookiejar

            for handle in list(d.window_handles):
                try:
                    d.switch_to.window(handle)
                    url = d.current_url or ""
                    ctype = ""
                    try:
                        ctype = (
                            d.execute_script("return document.contentType || '';") or ""
                        )
                    except Exception:
                        pass
                    is_pdf_url = bool(
                        re.search(r"\.pdf($|\?)", url, re.I)
                        or "application/pdf" in ctype.lower()
                        or re.search(r"export|relatorio|report|download", url, re.I)
                    )
                    if not is_pdf_url and url.startswith("blob:"):
                        is_pdf_url = True
                    if not is_pdf_url and handle == main:
                        continue
                    if not url or url in ("about:blank", "data:,"):
                        continue

                    if url.startswith("http"):
                        cj = http.cookiejar.CookieJar()
                        for c in d.get_cookies():
                            try:
                                ck = http.cookiejar.Cookie(
                                    version=0,
                                    name=c["name"],
                                    value=c["value"],
                                    port=None,
                                    port_specified=False,
                                    domain=c.get("domain") or "",
                                    domain_specified=bool(c.get("domain")),
                                    domain_initial_dot=(c.get("domain") or "").startswith(
                                        "."
                                    ),
                                    path=c.get("path") or "/",
                                    path_specified=True,
                                    secure=bool(c.get("secure")),
                                    expires=None,
                                    discard=True,
                                    comment=None,
                                    comment_url=None,
                                    rest={"HttpOnly": None},
                                    rfc2109=False,
                                )
                                cj.set_cookie(ck)
                            except Exception:
                                continue
                        opener = urllib.request.build_opener(
                            urllib.request.HTTPCookieProcessor(cj)
                        )
                        ua = "Mozilla/5.0"
                        try:
                            ua = d.execute_script("return navigator.userAgent;") or ua
                        except Exception:
                            pass
                        req = urllib.request.Request(url, headers={"User-Agent": ua})
                        with opener.open(req, timeout=90) as resp:
                            data = resp.read()
                        if data[:4] == b"%PDF" or len(data) > 2000:
                            out = dest / f"sitrax_tab_{int(time.time())}.pdf"
                            out.write_bytes(data)
                            if out.stat().st_size > 500:
                                saved = out
                                logger.info(
                                    "PDF capturado de aba: %s (%s bytes)",
                                    out.name,
                                    out.stat().st_size,
                                )
                                break
                except Exception as e:
                    logger.debug("Aba PDF: %s", e)
                    continue
        finally:
            try:
                if main in d.window_handles:
                    d.switch_to.window(main)
            except Exception:
                pass
        return saved

    def _wait_pdf_download(
        self,
        dest: Path,
        before: set[str],
        timeout: float = 120,
    ) -> Optional[Path]:
        """Espera PDF na pasta temp OU captura de nova aba."""
        end = time.time() + timeout
        last_partial = False
        popup_tries = 0
        while time.time() < end:
            # dialog "Your download will start..." pode bloquear
            if popup_tries < 6:
                try:
                    body = self._d().execute_script(
                        "return (document.body && document.body.innerText) || '';"
                    ) or ""
                    if "download will start" in body.lower() or "few seconds" in body.lower():
                        self._dismiss_download_popup()
                        popup_tries += 1
                except Exception:
                    pass
            partial = (
                list(dest.glob("*.crdownload"))
                + list(dest.glob("*.tmp"))
                + list(dest.glob("*.part"))
            )
            if partial:
                last_partial = True
                time.sleep(0.5)
                continue
            news = [p for p in dest.glob("*.pdf") if p.name not in before]
            if not news:
                for p in dest.iterdir():
                    if not p.is_file() or p.name in before:
                        continue
                    try:
                        if p.stat().st_size > 1000 and p.read_bytes()[:4] == b"%PDF":
                            news.append(p)
                    except Exception:
                        pass
            if news:
                newest = max(news, key=lambda p: p.stat().st_mtime)
                if newest.stat().st_size > 1000:
                    sz1 = newest.stat().st_size
                    time.sleep(0.6)
                    sz2 = newest.stat().st_size
                    if sz2 == sz1:
                        logger.info(
                            "PDF bruto baixado no servidor: %s (%s bytes)",
                            newest,
                            sz2,
                        )
                        return newest
            tab_pdf = self._try_save_pdf_from_open_tabs(dest)
            if tab_pdf and tab_pdf.exists() and tab_pdf.stat().st_size > 1000:
                return tab_pdf
            time.sleep(0.45)
        if last_partial:
            logger.warning("Download ficou em .crdownload ate o timeout")
        return None

    def download_historico_pdf(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        dest_dir: Optional[Path | str] = None,
        timeout: float = 120,
        already_filtered: bool = False,
    ) -> Optional[Path]:
        """
        Fluxo:
          posicoes -> veiculo -> filtrar -> nuvem -> PDF file
        PDF BRUTO so no servidor (temp). Retorna caminho do arquivo.
        """
        dest = Path(dest_dir) if dest_dir else self.download_dir
        if not dest:
            raise ValueError(
                "download_dir/dest_dir obrigatorio para PDF bruto no servidor"
            )
        dest.mkdir(parents=True, exist_ok=True)
        if self.download_dir is None:
            self.download_dir = dest
        self._ensure_download_behavior()

        before = {p.name for p in dest.glob("*") if p.is_file()}
        if not already_filtered:
            self._prepare_historico_filtrado(placa, data_ini, data_fim)

        handles_before = set(self._d().window_handles)
        self._click_download_cloud()

        pdf = self._wait_pdf_download(dest, before, timeout=timeout)
        if pdf:
            self._trace(
                "download_ok",
                f"PDF no servidor: {pdf.name} ({pdf.stat().st_size} bytes)",
                ok=True,
                shot=True,
            )
            return pdf

        d = self._d()
        new_handles = [h for h in d.window_handles if h not in handles_before]
        if new_handles:
            self._trace(
                "download_nova_aba",
                f"{len(new_handles)} aba(s) nova(s) — tentando capturar PDF",
                shot=True,
            )
            tab_pdf = self._try_save_pdf_from_open_tabs(dest)
            if tab_pdf:
                return tab_pdf

        self._save_debug(
            "download_timeout",
            f"Timeout esperando PDF em {dest} ({timeout}s)",
            ok=False,
        )
        raise TimeoutException(f"Timeout esperando PDF em {dest}")

    def get_positions_for_plate(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        already_on_posicoes: bool = False,
    ) -> list[Position]:
        if not already_on_posicoes:
            self._prepare_historico_filtrado(placa, data_ini, data_fim)
        else:
            self.click_filtrar()
        self.try_scroll_all()
        rows = self.scrape_positions_table()
        if not rows:
            self._save_debug(f"zero_posicoes_{placa}")
            logger.warning(
                "Nenhuma posição lida para %s — verifique se o veículo foi "
                "realmente aplicado no filtro.",
                placa,
            )
        return positions_from_rows(rows)

    def get_all_plates(self) -> list[dict]:
        self.open_posicoes()
        self.open_vehicle_selector()
        self.load_vehicle_list()
        vehicles = self.list_plates()
        try:
            cancel = self._find_first(
                [(By.XPATH, "//button[contains(., 'Cancelar')]")], timeout=3
            )
            self._click(cancel)
        except TimeoutException:
            pass
        return vehicles

    def report_for_plate(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
    ) -> tuple[str, list[Position]]:
        from app.bot.report import build_narrative_report

        positions = self.get_positions_for_plate(placa, data_ini, data_fim)
        data_ref = (data_ini or date.today()).strftime("%d/%m/%Y")
        if data_fim and data_fim != (data_ini or date.today()):
            data_ref += f" a {data_fim.strftime('%d/%m/%Y')}"
        text = build_narrative_report(placa, positions, data_ref=data_ref)
        return text, positions

    def report_all_vehicles(
        self,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        max_vehicles: int = 50,
    ) -> str:
        from app.bot.report import build_narrative_report

        vehicles = self.get_all_plates()
        # Últimas da lista primeiro (mais propensas a falhar no modal)
        vehicles = list(reversed(vehicles))
        parts: list[str] = []
        data_ref = (data_ini or date.today()).strftime("%d/%m/%Y")
        parts.append(f"📊 Relatório geral — {data_ref}")
        parts.append(f"Veículos na frota: {len(vehicles)} (ordem: últimas primeiro)\n")
        parts.append("=" * 40)

        for i, v in enumerate(vehicles[:max_vehicles]):
            placa = v["placa"]
            logger.info("Processando %s (%s/%s)", placa, i + 1, min(len(vehicles), max_vehicles))
            try:
                self.open_posicoes()
                self._close_date_popup_if_open()
                self.open_vehicle_selector()
                self.load_vehicle_list(placa=placa)
                self.select_vehicle_by_plate(placa)
                self.set_date_filter(data_ini, data_fim)
                self.click_filtrar()
                rows = self.scrape_positions_table()
                positions = positions_from_rows(rows)
                text = build_narrative_report(
                    placa, positions, data_ref=data_ref, cliente=v.get("cliente", "")
                )
                parts.append(text)
                parts.append("=" * 40)
            except Exception as e:
                parts.append(f"📋 {placa}: erro — {e}")
                parts.append("=" * 40)
                logger.exception("Erro em %s", placa)
        return "\n\n".join(parts)

    # aliases async usados pelo FastAPI
    async def login_async(self) -> None:
        self.login()

    async def report_for_plate_async(self, *a, **k):
        return self.report_for_plate(*a, **k)

    async def report_all_vehicles_async(self, *a, **k):
        return self.report_all_vehicles(*a, **k)
