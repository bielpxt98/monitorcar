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
    ):
        self.cliente = cliente or settings.sitrax_cliente
        self.usuario = usuario or settings.sitrax_usuario
        self.senha = senha or settings.sitrax_senha
        self.login_url = login_url or settings.sitrax_url
        self.headless = settings.sitrax_headless if headless is None else headless
        # Downloads do Sitrax só no servidor (pasta temp) — nunca no celular do usuário
        self.download_dir = Path(download_dir) if download_dir else None
        self.driver: Optional[webdriver.Chrome] = None
        self.wait: Optional[WebDriverWait] = None

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
            # headless "new" com viewport desktop (layout igual ao PC, não mobile)
            opts.add_argument("--headless=new")
        else:
            # local: janela grande, NÃO minimizada
            opts.add_argument("--start-maximized")
        # Flags críticas para Docker/Railway (evita "tab crashed" por /dev/shm e RAM)
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
        # viewport DESKTOP — o Sitrax esconde "Veículo"/filtros em largura pequena
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--window-position=0,0")
        opts.add_argument("--force-device-scale-factor=1")
        opts.add_argument("--high-dpi-support=1")
        opts.add_argument("--lang=pt-BR")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--renderer-process-limit=2")
        opts.add_argument("--js-flags=--max-old-space-size=384")
        # user-agent desktop (evita layout mobile no headless)
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        # perfil/cache em temp (Docker/Railway e Windows)
        import tempfile as _tmpmod

        _chrome_tmp = Path(_tmpmod.gettempdir()) / "sitrax-chrome"
        _chrome_tmp.mkdir(parents=True, exist_ok=True)
        opts.add_argument(f"--user-data-dir={_chrome_tmp / 'user-data'}")
        opts.add_argument(f"--disk-cache-dir={_chrome_tmp / 'cache'}")
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
                self.driver.set_window_size(1920, 1080)
        except Exception as e:
            logger.warning("Ajuste de janela: %s", e)

        # CDP: viewport desktop fixo (headless e Docker)
        try:
            self.driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 1920,
                    "height": 1080,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                    "screenWidth": 1920,
                    "screenHeight": 1080,
                },
            )
            self.driver.execute_cdp_cmd(
                "Emulation.setTouchEmulationEnabled",
                {"enabled": False},
            )
        except Exception as e:
            logger.warning("CDP desktop viewport: %s", e)

        # Chrome headless: força download dir via CDP
        if self.download_dir:
            try:
                self.driver.execute_cdp_cmd(
                    "Page.setDownloadBehavior",
                    {
                        "behavior": "allow",
                        "downloadPath": str(self.download_dir.resolve()),
                    },
                )
            except Exception as e:
                logger.warning("CDP download path: %s", e)

        self._trace(
            "chrome_pronto",
            f"Chrome desktop 1920x1080 headless={self.headless}",
            shot=True,
        )

    def close(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver = None
        self.wait = None

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
        try:
            debug_session.step(
                name,
                message,
                driver=self._d() if self.driver else None,
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

    def load_vehicle_list(self, placa: Optional[str] = None) -> None:
        """
        Calibração (foto /debug):
          - Muitas vezes a lista JÁ vem cheia ao abrir o modal.
          - Se já tem itens → NÃO clicar na lupa (clicar de novo pode esvaziar).
          - Só clica na lupa preta de Placa se a lista estiver vazia.
        """
        d = self._d()
        self._sleep(0.6)

        # 1) Espera a lista carregar sozinha (como na calibração bem-sucedida)
        for i in range(12):
            n = self._count_vehicle_items()
            if n >= 1:
                msg = f"Lista já pronta com {n} veículo(s) — NÃO clicar na lupa"
                logger.info(msg)
                self._trace("lista_ja_pronta", msg, ok=True, shot=True)
                return
            self._sleep(0.4)

        # 2) Ainda vazia → uma única vez a lupa preta de Placa
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
        self._sleep(1.2)

        end = time.time() + 25
        while time.time() < end:
            n = self._count_vehicle_items()
            if n >= 1:
                msg = f"Veículos após lupa Placa: {n} item(ns)"
                logger.info(msg)
                self._save_debug("lista_apos_lupa_placa", msg, ok=True)
                return
            self._sleep(0.4)

        # 3) Ainda vazio: se tem placa, tenta filtrar digitando (sem outra lupa roxa)
        placa_u = re.sub(r"[^A-Z0-9]", "", (placa or "").upper())
        if placa_u:
            self._trace(
                "lista_ainda_vazia_digita_placa",
                f"Digitando placa {placa_u} no filtro e filtrando",
                ok=True,
                shot=True,
            )
            try:
                inp = d.find_element(
                    By.CSS_SELECTOR, "input[id='formModalSearchVeiculo:itCveiPlaca']"
                )
                d.execute_script(
                    "arguments[0].value=''; arguments[0].value=arguments[1];",
                    inp,
                    placa_u,
                )
                try:
                    inp.clear()
                    inp.send_keys(placa_u)
                except Exception:
                    pass
                d.execute_script(
                    "if (typeof swClick === 'function') "
                    "swClick('formModalSearchVeiculo:btnFiltrarVeiculo');"
                )
                self._wait_loader_gone(30)
                self._sleep(1)
                if self._count_vehicle_items() >= 1 or self._find_vehicle_item(placa_u):
                    self._trace("lista_ok_apos_digitar", f"Achou {placa_u}", ok=True)
                    return
            except Exception as e:
                logger.warning("Filtro por placa digitada: %s", e)

        self._save_debug(
            "lista_vazia_apos_lupa_placa",
            "Lista continua vazia após lupa (e filtro por placa)",
            ok=False,
        )
        raise TimeoutException(
            "Modal aberto mas a lista de veículos não carregou. "
            "Se a lupa já tinha lista, não deveria clicar de novo — "
            "abra /debug para calibrar."
        )

    def list_plates(self) -> list[dict]:
        """Lista placas do modal (div.swModalContentListItem + cadVeiculoSearchSelect)."""
        d = self._d()
        vehicles: list[dict] = []
        items = d.find_elements(
            By.CSS_SELECTOR, "div[onclick*='cadVeiculoSearchSelect'], div.swModalContentListItem"
        )
        for i, item in enumerate(items):
            try:
                oc = item.get_attribute("onclick") or ""
                m = re.search(r"'([A-Z]{3}\d[A-Z0-9]\d{2}|[A-Z]{3}\d{4})'", oc)
                texts = [
                    s.text.strip()
                    for s in item.find_elements(By.CSS_SELECTOR, "span.swMiniModalItemsText")
                ]
                placa = m.group(1) if m else ""
                if not placa and texts:
                    mm = re.search(
                        r"([A-Z]{3}\d[A-Z0-9]\d{2}|[A-Z]{3}\d{4})",
                        " ".join(texts).upper(),
                    )
                    placa = mm.group(1) if mm else ""
                if not placa:
                    continue
                vehicles.append(
                    {
                        "placa": placa,
                        "display": texts[1] if len(texts) > 1 else "",
                        "cliente": texts[2] if len(texts) > 2 else "",
                        "serial": texts[3] if len(texts) > 3 else "",
                        "index": i,
                    }
                )
            except StaleElementReferenceException:
                continue
        logger.info("Veículos: %s", len(vehicles))
        return vehicles

    def _find_vehicle_item(self, placa_u: str):
        """
        Encontra o item do modal Sitrax para a placa.
        Estrutura real:
          <div id="1314149_idDivSearchVeiculo" class="swModalContentListItem"
               onclick="cadVeiculoSearchSelect(..., 'PCE7B03', '1314149_btn');">
            <input id="1314149_btn" type="checkbox" class="swCheckBoxCustom">
            <span>PCE7B03</span>
          </div>
        """
        d = self._d()
        # 1) pelo onclick da função nativa do Sitrax (mais confiável)
        for el in d.find_elements(
            By.CSS_SELECTOR, "div[onclick*='cadVeiculoSearchSelect']"
        ):
            try:
                oc = el.get_attribute("onclick") or ""
                if f"'{placa_u}'" in oc or f'"{placa_u}"' in oc:
                    if el.is_displayed():
                        return el
            except StaleElementReferenceException:
                continue

        # 2) XPath contains no onclick
        for el in d.find_elements(
            By.XPATH,
            f"//div[contains(@onclick,\"'{placa_u}'\") or contains(@onclick,'{placa_u}')]",
        ):
            try:
                if el.is_displayed():
                    return el
            except Exception:
                continue

        # 3) span com texto da placa → sobe até o list item
        for el in d.find_elements(
            By.XPATH,
            f"//span[normalize-space()='{placa_u}']/ancestor::div[contains(@class,'swModalContentListItem') or contains(@onclick,'cadVeiculoSearchSelect')][1]",
        ):
            try:
                if el.is_displayed():
                    return el
            except Exception:
                continue

        # 4) texto visível
        for el in d.find_elements(By.CSS_SELECTOR, "div.swModalContentListItem"):
            try:
                if el.is_displayed() and placa_u in (el.text or "").upper().replace(
                    "-", ""
                ).replace(" ", ""):
                    return el
            except Exception:
                continue
        return None

    def select_vehicle_by_plate(self, placa: str) -> dict:
        """
        Seleciona veículo no modal Sitrax via cadVeiculoSearchSelect + checkbox,
        depois selectVeiculoSearch() / botão Selecionar.
        """
        d = self._d()
        placa_u = re.sub(r"[^A-Z0-9]", "", placa.upper())
        logger.info("Selecionando placa %s (modo Sitrax div/checkbox)", placa_u)

        # espera lista (divs) aparecer
        item = None
        end = time.time() + 25
        while time.time() < end:
            if self._vehicle_rows_visible():
                item = self._find_vehicle_item(placa_u)
                if item is not None:
                    break
            time.sleep(0.4)

        if item is None:
            item = self._find_vehicle_item(placa_u)

        if item is None:
            self._save_debug(f"placa_nao_encontrada_{placa_u}")
            # lista quantas placas o JS enxerga
            try:
                n = d.execute_script(
                    "return document.querySelectorAll(\"div[onclick*='cadVeiculoSearchSelect']\").length;"
                )
            except Exception:
                n = "?"
            raise NoSuchElementException(
                f"Placa {placa_u} não encontrada no modal "
                f"({n} itens cadVeiculoSearchSelect). "
                f"Veja {DEBUG_DIR}"
            )

        # --- marcar via função nativa do Sitrax (melhor que clicar no rádio genérico) ---
        selected_ok = False
        try:
            oc = item.get_attribute("onclick") or ""
            # tenta executar o onclick inteiro
            d.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", item
            )
            d.execute_script(oc if oc.strip().endswith(";") else oc + ";")
            selected_ok = True
            logger.info("Executou cadVeiculoSearchSelect via onclick para %s", placa_u)
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
            cb = item.find_element(By.CSS_SELECTOR, "input[type='checkbox'], input.swCheckBoxCustom")
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

        # também clica no botão visual (onclick="selectVeiculoSearch(); hideModalSearchVeiculo();")
        sel = None
        for el in d.find_elements(
            By.XPATH,
            "//*[contains(@onclick,'selectVeiculoSearch')] | "
            "//button[contains(.,'Selecionar') or contains(.,'Select')] | "
            "//a[contains(.,'Selecionar') or contains(.,'Select')] | "
            "//span[normalize-space()='Selecionar' or normalize-space()='Select']/ancestor::*[self::button or self::a or self::div][1]",
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

        # modal deve sumir
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
            body = d.find_element(By.TAG_NAME, "body").text
            if (
                "Selecione Veículo" in body
                or "Select Vehicle" in body
            ):
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
        """Clica no botão laranja Filtrar/Filter da barra principal."""
        d = self._d()
        self._close_date_popup_if_open()
        btn = None
        for el in d.find_elements(
            By.XPATH,
            "//button[contains(normalize-space(.),'Filtrar') or contains(normalize-space(.),'Filter')] | "
            "//a[contains(normalize-space(.),'Filtrar') or contains(normalize-space(.),'Filter')]",
        ):
            try:
                if not el.is_displayed():
                    continue
                parent_txt = ""
                try:
                    parent_txt = el.find_element(
                        By.XPATH,
                        "./ancestor::*[contains(@class,'modal') or contains(@class,'popup') or contains(@class,'dropdown')][1]",
                    ).text
                except Exception:
                    parent_txt = ""
                if "Filtro Data" in parent_txt or "Início" in parent_txt or "Date Filter" in parent_txt:
                    continue
                btn = el
                cls = el.get_attribute("class") or ""
                if "orange" in cls.lower() or "primary" in cls.lower() or "filtrar" in cls.lower() or "filter" in cls.lower():
                    break
            except Exception:
                continue
        if not btn:
            btn = self._find_first(
                [
                    (By.XPATH, "//button[contains(., 'Filtrar') or contains(., 'Filter')]"),
                    (By.XPATH, "//a[contains(., 'Filtrar') or contains(., 'Filter')]"),
                ],
                timeout=10,
            )
        self._click(btn)
        self._wait_loader_gone(40)
        self._sleep(1.5)

    def scrape_positions_table(self) -> list[dict]:
        d = self._d()
        self._sleep(1)
        tables = d.find_elements(By.CSS_SELECTOR, "table")
        if not tables:
            return []

        # pega a maior tabela com dados
        table = max(tables, key=lambda t: len(t.find_elements(By.CSS_SELECTOR, "tbody tr")))
        headers = [th.text.strip() for th in table.find_elements(By.CSS_SELECTOR, "thead th")]
        if not headers:
            first = table.find_elements(By.CSS_SELECTOR, "tr")[0]
            headers = [c.text.strip() for c in first.find_elements(By.CSS_SELECTOR, "th, td")]

        def find_col(*names: str) -> Optional[int]:
            for i, h in enumerate(headers):
                hl = h.lower()
                for n in names:
                    if n.lower() in hl:
                        return i
            return None

        idx_gps = find_col("Data GPS", "GPS")
        idx_sis = find_col("Data Sistema", "Sistema")
        idx_modo = find_col("Modo")
        idx_end = find_col("Endereço", "Endereco")
        idx_ref = find_col("Referência", "Referencia")
        idx_temp = find_col("Temperatura")

        rows_data: list[dict] = []
        for row in table.find_elements(By.CSS_SELECTOR, "tbody tr"):
            cells = row.find_elements(By.TAG_NAME, "td")
            if not cells:
                continue
            texts = [c.text.strip() for c in cells]

            def cell(idx: Optional[int]) -> str:
                if idx is None or idx >= len(texts):
                    return ""
                return texts[idx]

            item = {
                "data_gps": cell(idx_gps) if idx_gps is not None else (texts[0] if texts else ""),
                "data_sistema": cell(idx_sis) if idx_sis is not None else (texts[1] if len(texts) > 1 else ""),
                "modo": cell(idx_modo),
                "endereco": cell(idx_end),
                "referencia": cell(idx_ref),
                "temperatura": cell(idx_temp),
                "raw_cells": texts,
            }
            if not item["endereco"]:
                for t in texts:
                    if re.search(r"(rua|avenida|av\.|rodovia|estrada)", t, re.I):
                        item["endereco"] = t
                        break
            if not item["modo"]:
                for t in texts:
                    if re.search(r"(estacionado|normal|alerta|igni|emerg)", t, re.I):
                        item["modo"] = t
                        break
            if not re.search(r"\d{2}/\d{2}/\d{4}", item.get("data_gps") or ""):
                for t in texts:
                    if re.search(r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}", t):
                        item["data_gps"] = t
                        break
            rows_data.append(item)

        logger.info("Posições lidas: %s", len(rows_data))
        return rows_data

    def try_scroll_all(self) -> None:
        d = self._d()
        for _ in range(8):
            d.execute_script("window.scrollBy(0, 1200);")
            self._sleep(0.3)

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

    def download_historico_pdf(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        dest_dir: Optional[Path | str] = None,
        timeout: float = 90,
    ) -> Optional[Path]:
        """
        Fluxo:
          posições → veículo → filtrar → clicar nuvem/download
        O PDF BRUTO cai em dest_dir (temp no servidor) — NÃO no celular.
        Retorna o caminho do arquivo baixado.
        """
        dest = Path(dest_dir) if dest_dir else self.download_dir
        if not dest:
            raise ValueError("download_dir/dest_dir obrigatório para PDF bruto no servidor")
        dest.mkdir(parents=True, exist_ok=True)

        before = {p.name for p in dest.glob("*.pdf")}
        self._prepare_historico_filtrado(placa, data_ini, data_fim)

        # botão download (nuvem roxa ao lado de Filtrar) — via JS no headless
        clicked = False
        try:
            res = self._d().execute_script(
                """
                // ícones de nuvem / download
                var sels = [
                  'i.fa-cloud-arrow-down', 'i.fa-cloud-download', 'i.fa-cloud-download-alt',
                  'i.fa-download', 'i[class*="cloud"]', 'i[class*="download"]',
                  '[onclick*="export"]', '[onclick*="Export"]', '[onclick*="download"]',
                  '[onclick*="Download"]', '[title*="Download"]', '[title*="export"]'
                ];
                for (var s = 0; s < sels.length; s++) {
                  var nodes = document.querySelectorAll(sels[s]);
                  for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    if (el.closest && el.closest('#sidebar-menu')) continue;
                    var r = el.getBoundingClientRect();
                    if (r.width < 1 && r.height < 1) continue;
                    el.scrollIntoView({block:'center'});
                    el.click();
                    return sels[s];
                  }
                }
                // botão/link roxo ao lado de Filtrar
                var all = document.querySelectorAll('button,a,div,span,i');
                for (var j = 0; j < all.length; j++) {
                  var e = all[j];
                  var cls = (e.className || '') + '';
                  var oc = e.getAttribute('onclick') || '';
                  if (/cloud|download|export/i.test(cls + ' ' + oc)) {
                    if (e.closest && e.closest('#sidebar-menu')) continue;
                    e.click();
                    return 'heuristic';
                  }
                }
                return null;
                """
            )
            if res:
                clicked = True
                logger.info("Clicou download via JS: %s", res)
        except Exception as e:
            logger.warning("JS download: %s", e)

        if not clicked:
            for sel in [
                (By.CSS_SELECTOR, "i.fa-cloud-arrow-down, i.fa-cloud-download-alt, i.fa-download, i[class*='cloud']"),
                (By.CSS_SELECTOR, "[onclick*='export'], [onclick*='Export'], [onclick*='download']"),
            ]:
                try:
                    for el in self._d().find_elements(*sel):
                        try:
                            self._d().execute_script("arguments[0].click();", el)
                            clicked = True
                            break
                        except Exception:
                            continue
                except Exception:
                    continue
                if clicked:
                    break

        if not clicked:
            self._save_debug("download_nao_encontrado")
            raise TimeoutException(
                "Não achei o botão de download (nuvem) do histórico. "
                f"Veja {DEBUG_DIR}"
            )

        # espera PDF aparecer na pasta temp
        end = time.time() + timeout
        while time.time() < end:
            pdfs = list(dest.glob("*.pdf"))
            # ignora .crdownload
            partial = list(dest.glob("*.crdownload")) + list(dest.glob("*.tmp"))
            if partial:
                time.sleep(0.5)
                continue
            new = [p for p in pdfs if p.name not in before]
            if new:
                newest = max(new, key=lambda p: p.stat().st_mtime)
                if newest.stat().st_size > 1000:
                    logger.info("PDF bruto baixado no servidor: %s", newest)
                    return newest
            time.sleep(0.5)

        self._save_debug("download_timeout")
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
        parts: list[str] = []
        data_ref = (data_ini or date.today()).strftime("%d/%m/%Y")
        parts.append(f"📊 Relatório geral — {data_ref}")
        parts.append(f"Veículos na frota: {len(vehicles)}\n")
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
