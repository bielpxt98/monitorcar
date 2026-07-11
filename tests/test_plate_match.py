"""Testes unitários do match de placa (sem Selenium)."""

from app.bot.sitrax import SitraxBot


def test_norm_placa_strips_noise():
    assert SitraxBot._norm_placa("abc-1d23") == "ABC1D23"
    assert SitraxBot._norm_placa(" ABC 1234 ") == "ABC1234"


def test_extract_from_cad_veiculo_onclick():
    bot = SitraxBot.__new__(SitraxBot)
    oc = "cadVeiculoSearchSelect('SOK7A35', '1', 'foo');"
    assert bot._extract_plate_token(oc) == "SOK7A35"


def test_extract_mercosul_and_old():
    bot = SitraxBot.__new__(SitraxBot)
    assert bot._extract_plate_token("foo 'PDY4D85' bar") == "PDY4D85"
    assert bot._extract_plate_token("placa RZN4132 ok") == "RZN4132"
    assert bot._extract_plate_token("ABC1234") == "ABC1234"


def test_extract_does_not_confuse_partial():
    bot = SitraxBot.__new__(SitraxBot)
    # deve pegar o token de placa completo, não lixo
    oc = "cadVeiculoSearchSelect('PCX5F06', 12, true)"
    assert bot._extract_plate_token(oc) == "PCX5F06"
