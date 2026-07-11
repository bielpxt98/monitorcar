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
        """Fecha o popup 'Filtro Data' se estiver aberto (evita clicar errado)."""
        d = self._d()
        try:
            # se o popup de data está visível, clica Fechar
            popups = d.find_elements(
                By.XPATH,
                "//*[contains(normalize-space(.),'Filtro Data') or contains(normalize-space(.),'Filtro data')]",
            )
            if not any(p.is_displayed() for p in popups):
                return
            for xp in [
                "//button[normalize-space()='Fechar']",
                "//a[normalize-space()='Fechar']",
                "//*[normalize-space()='Fechar' and (self::button or self::a or self::span)]",
            ]:
                for el in d.find_elements(By.XPATH, xp):
                    try:
                        if el.is_displayed():
                            self._click(el)
                            self._sleep(0.4)
                            logger.info("Popup de data fechado")
                            return
                    except Exception:
                        continue
            # ESC
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

    def set_date_filter(
        self,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
    ) -> None:
        """
        Ajusta a data só se for necessário.
        No Sitrax o filtro é um chip 'Data: ...' — NÃO clicar nele
        se a data do dia já estiver correta (evita abrir popup no lugar de Veículo).
        """
        d = self._d()
        data_ini = data_ini or date.today()
        data_fim = data_fim or date.today()
        ini_br = data_ini.strftime("%d/%m/%Y")
        fim_br = data_fim.strftime("%d/%m/%Y")

        self._close_date_popup_if_open()

        try:
            body = d.find_element(By.TAG_NAME, "body").text
        except Exception:
            body = ""

        # Se a barra já mostra o período desejado, não mexe (padrão: hoje)
        if ini_br in body and fim_br in body:
            logger.info("Data já está no filtro: %s → %s (sem clicar)", ini_br, fim_br)
            return

        # Precisa mudar: clica no chip de Data/Date (não em Veículo/Vehicle)
        date_chip = None
        for el in d.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.),'Data:') or starts-with(normalize-space(.),'Data') "
            "or contains(normalize-space(.),'Date:') or starts-with(normalize-space(.),'Date')]",
        ):
            try:
                if not el.is_displayed():
                    continue
                t = (el.text or "").strip()
                if (
                    ("Data" in t or "Date" in t)
                    and re.search(r"\d{2}/\d{2}/\d{4}", t)
                ):
                    if el.size.get("height", 99) < 60:
                        date_chip = el
                        break
                    if date_chip is None:
                        date_chip = el
            except Exception:
                continue

        if not date_chip:
            logger.warning("Não achou chip de Data/Date; mantendo data atual do sistema")
            return

        self._click(date_chip)
        self._sleep(0.6)

        # preenche Início / Fim no popup se houver inputs
        inputs = d.find_elements(By.CSS_SELECTOR, "input")
        visible_inputs = [i for i in inputs if i.is_displayed()]
        filled = 0
        for inp in visible_inputs:
            try:
                val = (inp.get_attribute("value") or "")
                # preenche os dois primeiros campos de data do popup
                if filled == 0:
                    inp.clear()
                    inp.send_keys(f"{ini_br} 00:00:00")
                    filled = 1
                elif filled == 1:
                    inp.clear()
                    inp.send_keys(f"{fim_br} 23:59:59")
                    filled = 2
                    break
            except Exception:
                continue

        # Filtrar / Filter dentro do popup de data
        for el in d.find_elements(
            By.XPATH,
            "//button[normalize-space()='Filtrar' or normalize-space()='Filter']",
        ):
            try:
                if el.is_displayed():
                    self._click(el)
                    break
            except Exception:
                continue

        self._wait_loader_gone(20)
        self._close_date_popup_if_open()
        logger.info("Data filtro ajustada: %s → %s", ini_br, fim_br)

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
        """True se a barra de filtros mostra o chip Veículo com essa placa."""
        placa_u = self._norm_placa(placa)
        if not placa_u:
            return False
        try:
            return bool(
                self._d().execute_script(
                    """
                    var alvo = (arguments[0] || '').toUpperCase().replace(/[^A-Z0-9]/g,'');
                    if (!alvo) return false;
                    var body = ((document.body && document.body.innerText) || '').toUpperCase();
                    // chip "VEÍCULO: PDY4D85" / "VEHICLE: PDX3G64"
                    if (body.indexOf(alvo) < 0) return false;
                    var re = new RegExp(
                      'VE[IÍ]CULO\\\\s*:\\\\s*' + alvo + '|' +
                      'VEHICLE\\\\s*:\\\\s*' + alvo
                    );
                    if (re.test(body)) return true;
                    // fallback: placa aparece perto da barra de filtros
                    var nodes = document.querySelectorAll('div,span,button,a,label,li');
                    for (var i = 0; i < nodes.length; i++) {
                      var t = (nodes[i].innerText || nodes[i].textContent || '')
                        .replace(/\\s+/g,' ').trim().toUpperCase();
                      if (t.length > 60) continue;
                      if (t.indexOf(alvo) >= 0 &&
                          (t.indexOf('VEICULO') >= 0 || t.indexOf('VEÍCULO') >= 0 ||
                           t.indexOf('VEHICLE') >= 0 || /^[A-Z]{3}/.test(t))) {
                        return true;
                      }
                    }
                    return false;
                    """,
                    placa_u,
                )
            )
        except Exception:
            return False

    def wait_positions_grid(self, timeout: float = 30) -> int:
        """
        Espera a grade de posições (PT/EN).
        Conta linhas com data GPS (não só tr vazios do DataTables).
        """
        d = self._d()
        end = time.time() + timeout
        last_n = 0
        while time.time() < end:
            try:
                n = d.execute_script(
                    """
                    var body = (document.body && document.body.innerText) || '';
                    var m = body.match(/Mostrando\\s*:\\s*(\\d+)/i)
                         || body.match(/Showing\\s*:\\s*(\\d+)/i)
                         || body.match(/(\\d+)\\s*Registro/i)
                         || body.match(/(\\d+)\\s*Record/i);
                    if (m && parseInt(m[1], 10) > 0) return parseInt(m[1], 10);

                    // conta linhas com data dd/mm/yyyy hh:mm (scrollBody ou qualquer table)
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
                if re.search(
                    r"mostrando\s*:\s*0|showing\s*:\s*0|0\s*registro|0\s*record",
                    (d.find_element(By.TAG_NAME, "body").text or ""),
                    re.I,
                ):
                    # só aceita zero explícito depois de ~4s de espera
                    if time.time() + timeout - end > 4:
                        return 0
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
          limpa chip se preciso → escolhe placa → Filter → espera grade → scrape.
        Até 3 tentativas se voltar 0 linhas (problema típico da frota).
        """
        from app.bot.report import positions_from_rows

        placa_u = self._norm_placa(placa)
        last_rows: list = []
        for attempt in range(3):
            try:
                # tentativa 0: clear se pedido; tentativas seguintes sempre limpam
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

            # modal fechado?
            try:
                self._d().execute_script(
                    "if (typeof hideModalSearchVeiculo === 'function') "
                    "hideModalSearchVeiculo();"
                )
            except Exception:
                pass
            self._sleep(0.4)

            if not self.vehicle_chip_has_plate(placa_u):
                logger.warning(
                    "Chip Veículo sem %s após select (tentativa %s) — refaz",
                    placa_u,
                    attempt + 1,
                )
                self._trace(
                    f"chip_sem_{placa_u}",
                    f"Chip não mostrou {placa_u}",
                    ok=False,
                    shot=True,
                )
                self.clear_vehicle_chip()
                continue

            # garante Filter de novo (às vezes o 1º click some com modal)
            try:
                self.click_filtrar()
            except Exception as e:
                logger.warning("re-Filter %s: %s", placa_u, e)

            n_hint = self.wait_positions_grid(timeout=28)
            self.try_scroll_all()
            rows = self.scrape_positions_table()
            last_rows = rows or []
            n_scrape = len(last_rows)
            logger.info(
                "Frota %s tentativa %s: grid~%s scrape=%s",
                placa_u,
                attempt + 1,
                n_hint,
                n_scrape,
            )
            if n_scrape > 0:
                self._trace(
                    f"frota_rows_{placa_u}",
                    f"{n_scrape} linha(s) scrapadas (hint grid {n_hint})",
                    ok=True,
                )
                return last_rows

            # 0 linhas: tenta Filter + espera de novo
            try:
                self.click_filtrar()
                self.wait_positions_grid(timeout=15)
                self.try_scroll_all()
                rows = self.scrape_positions_table()
                last_rows = rows or []
                if last_rows:
                    return last_rows
            except Exception:
                pass

            self._save_debug(
                f"frota_zero_{placa_u}_t{attempt+1}",
                f"0 posições para {placa_u} tentativa {attempt+1}",
                ok=False,
            )

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
