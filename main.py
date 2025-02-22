from flask import Flask, request, jsonify, render_template_string, send_from_directory, send_file, redirect, url_for
import os
import re
import pdfplumber
import pandas as pd
from datetime import datetime
import io
import logging

# Configurar logging
app = Flask(__name__)
app.logger.setLevel(logging.INFO)  # Logs para depuração no Render
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # Limite de 5MB por upload

# Usar variável de ambiente para secret_key
app.secret_key = os.getenv("FLASK_SECRET_KEY", "sua_chave_secreta_aqui")

# Certifique-se de que as pastas "temp" e "reports" existam
os.makedirs("temp", exist_ok=True)
os.makedirs("reports", exist_ok=True)

# ========================================
# Funções de Extração de Texto (Somente PDFs)
# ========================================
def extrair_texto_pdf(caminho_arquivo, max_paginas=5):
    texto = ""
    try:
        with pdfplumber.open(caminho_arquivo) as pdf:
            for i, pagina in enumerate(pdf.pages):
                if i >= max_paginas:  # Limitar a 5 páginas pra performance
                    break
                conteudo = pagina.extract_text()
                if conteudo:
                    texto += conteudo + "\n"
    except Exception as e:
        app.logger.error(f"Erro ao extrair texto do PDF: {e}")
    return texto

def extrair_texto(caminho_arquivo):
    ext = os.path.splitext(caminho_arquivo)[1].lower()
    if ext == ".pdf":
        return extrair_texto_pdf(caminho_arquivo)
    else:
        return "Formato não suportado. Envie um PDF."

# ========================================
# Validação de Arquivo
# ========================================
def validar_arquivo(caminho_arquivo):
    if os.path.getsize(caminho_arquivo) > app.config['MAX_CONTENT_LENGTH']:
        return False, "Arquivo muito grande. Máximo 5MB."
    ext = os.path.splitext(caminho_arquivo)[1].lower()
    if ext != ".pdf":
        return False, "Formato não suportado. Envie apenas PDFs."
    texto = extrair_texto(caminho_arquivo)
    if len(texto.strip()) < 50:  # Threshold ajustável pra qualidade mínima
        return False, "Qualidade do arquivo baixa. Texto insuficiente para análise."
    return True, "Arquivo válido."

# ========================================
# Verificação da Qualidade do Texto
# ========================================
def verificar_qualidade_texto(texto, threshold=50):
    if len(texto.strip()) < threshold:
        return "Baixa qualidade: texto insuficiente para análise."
    return "OK"

# ========================================
# Extração de Dados dos Documentos
# ========================================
def extrair_cpf(texto):
    padrao = r'(\d{3}\.\d{3}\.\d{3}-\d{2})'
    resultado = re.search(padrao, texto)
    if resultado:
        return resultado.group(1)
    padrao2 = r'(\d{11})'
    resultado2 = re.search(padrao2, texto)
    if resultado2:
        return f"{resultado2.group(1)[:3]}.{resultado2.group(1)[3:6]}.{resultado2.group(1)[6:9]}-{resultado2.group(1)[9:]}"
    return "Não encontrado"

def extrair_nome(texto):
    padrao = r'Nome:\s*([A-Za-zÀ-ÿ\s]+)'
    resultado = re.search(padrao, texto, re.IGNORECASE)
    if resultado:
        return resultado.group(1).strip().title()  # Padronizar pra maiúscula inicial
    return "Não encontrado"

def extrair_pis(texto):
    padrao = r'(PIS\/PASEP[:\s]*\d{3}\.\d{5}\.\d{2}-\d{1})'
    resultado = re.search(padrao, texto)
    if resultado:
        return resultado.group(1)
    return "Não encontrado"

def extrair_contribuicoes(texto):
    contribs = {}
    padrao = r'(\d{4}-\d{2}):\s*([\d.,]+)'
    matches = re.findall(padrao, texto)
    for data_str, valor_str in matches:
        try:
            valor = float(valor_str.replace('.', '').replace(',', '.'))
            contribs[data_str] = round(valor, 2)  # Padronizar pra 2 casas decimais
        except Exception as e:
            app.logger.warning(f"Erro ao processar contribuição {data_str}: {e}")
            continue
    return contribs

# ========================================
# Validação de CPF
# ========================================
def validar_cpf(cpf):
    cpf = re.sub(r'\D', '', cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    def calcular_digito(cpf_parcial, peso_inicial):
        total = 0
        for digito in cpf_parcial:
            total += int(digito) * peso_inicial
            peso_inicial -= 1
        resto = total % 11
        return '0' if resto < 2 else str(11 - resto)
    digito1 = calcular_digito(cpf[:9], 10)
    digito2 = calcular_digito(cpf[:9] + digito1, 11)
    return cpf[-2:] == digito1 + digito2

# ========================================
# Cálculo do Benefício
# ========================================
def calcular_beneficio(contribuicoes, salario_min=1412.00):
    if not contribuicoes:
        return None
    datas = sorted(contribuicoes.keys(), reverse=True)[:12]
    valores = [contribuicoes[data] for data in datas if data in contribs]
    if not valores:
        return None
    media = sum(valores) / len(valores)
    return round(max(media, salario_min), 2)

# ========================================
# Verificação de Elegibilidade e Documentos Obrigatórios
# ========================================
def verificar_elegibilidade(dados):
    elegibilidade = {}
    contribs = dados.get("contribuicoes", {})
    pagou_12 = len(contribs) >= 12
    elegibilidade["pagou_12_meses"] = pagou_12
    elegibilidade["qualidade_segurada"] = True  # Baseado em diretrizes públicas genéricas
    elegibilidade["dentro_prazo"] = True  # Assume que está dentro do prazo (ajuste conforme INSS)
    if not pagou_12:
        elegibilidade["mensagem"] = "A cliente não está apta a prosseguir. É necessário ter contribuído por 12 meses."
    else:
        elegibilidade["mensagem"] = "A cliente está apta a prosseguir."
    return elegibilidade

def verificar_documentos_obrigatorios(dados):
    obrigatorios = ["rg", "certidao", "cnis", "pis"]
    ocupacao = dados.get("ocupacao", "")
    if ocupacao == "MEI":
        obrigatorios.append("mei")
    faltantes = [doc.upper() for doc in obrigatorios if not dados.get(doc)]
    return faltantes

# ========================================
# Cálculo da Aprovação (Escala)
# ========================================
def calcular_aprovacao(dados, ocupacao):
    score = 0
    if dados.get("elegibilidade", {}).get("pagou_12_meses"):
        score += 2
    if dados.get("cpf_valido"):
        score += 1
    if not verificar_documentos_obrigatorios(dados):
        score += 1
    if ocupacao == "CLT":
        score += 1
    elif ocupacao in ["MEI", "Autônoma", "Desempregada"]:
        score += 0.5
    if score < 2:
        return "Benefício Improvável"
    elif score < 3:
        return "Pouco provável"
    elif score < 4:
        return "Provável"
    else:
        return "Muito provável"

# ========================================
# Renomeação dos Arquivos (Padronizada)
# ========================================
def renomear_arquivo(caminho_arquivo, cpf, nome, tipo_doc):
    diretorio = os.path.dirname(caminho_arquivo)
    extensao = os.path.splitext(caminho_arquivo)[1]
    cpf_limpo = re.sub(r'\D', '', cpf)
    nome_limpo = re.sub(r'[^a-zA-Z0-9]', '', nome.lower())[:20]  # Apenas letras/números, limite de 20 chars
    novo_nome = f"{cpf_limpo}_{nome_limpo}_{tipo_doc.lower()}{extensao}"
    novo_caminho = os.path.join(diretorio, novo_nome)
    try:
        os.rename(caminho_arquivo, novo_caminho)
    except Exception as e:
        app.logger.error(f"Erro ao renomear {caminho_arquivo}: {e}")
    return novo_caminho

# ========================================
# Geração do Relatório Final
# ========================================
def gerar_relatorio(dados):
    relatorio = {
        "rg": dados.get("rg"),
        "certidao": dados.get("certidao"),
        "cnis": dados.get("cnis"),
        "pis": dados.get("pis"),
        "mei": dados.get("mei"),
        "nome": dados.get("nome"),
        "cpf": dados.get("cpf"),
        "cpf_valido": dados.get("cpf_valido"),
        "beneficio_estimado": dados.get("beneficio"),
        "elegibilidade": verificar_elegibilidade(dados),
        "documentos_processados": dados.get("documentos"),
        "informacoes_incompletas": verificar_documentos_obrigatorios(dados),
        "ocupacao": dados.get("ocupacao"),
        "aprovacao": calcular_aprovacao(dados, dados.get("ocupacao", ""))
    }
    return relatorio

# ========================================
# Processamento dos Documentos
# ========================================
def processar_documentos(form_files, form_data):
    dados_aggregados = {}
    documentos_processados = []
    for campo in ['rg', 'certidao', 'cnis', 'pis', 'mei']:
        file = form_files.get(campo)
        if file:
            nome_arquivo = file.filename
            caminho_temp = os.path.join("temp", nome_arquivo)
            file.save(caminho_temp)
            is_valid, message = validar_arquivo(caminho_temp)
            if not is_valid:
                app.logger.warning(f"Arquivo inválido para {campo}: {message}")
                continue
            texto = extrair_texto(caminho_temp)
            qualidade = verificar_qualidade_texto(texto)
            info_doc = {"tipo": campo.upper(), "arquivo_original": nome_arquivo, "qualidade": qualidade}
            if campo in ['rg', 'certidao']:
                nome = extrair_nome(texto)
                info_doc["nome"] = nome
                dados_aggregados["nome"] = nome
                cpf = extrair_cpf(texto)
                info_doc["cpf"] = cpf
                if cpf:
                    info_doc["cpf_valido"] = validar_cpf(cpf)
                    dados_aggregados["cpf"] = cpf
                    dados_aggregados["cpf_valido"] = info_doc.get("cpf_valido")
            elif campo == "cnis":
                contribs = extrair_contribuicoes(texto)
                info_doc["contribuicoes"] = contribs
                dados_aggregados.setdefault("contribuicoes", {}).update(contribs)
            elif campo == "pis":
                pis = extrair_pis(texto)
                info_doc["pis"] = pis
                dados_aggregados["pis"] = pis
            elif campo == "mei":
                info_doc["mei"] = "Enviado"
                dados_aggregados["mei"] = "Enviado"
            if dados_aggregados.get("cpf") and dados_aggregados.get("nome"):
                novo_caminho = renomear_arquivo(caminho_temp, dados_aggregados["cpf"], dados_aggregados["nome"], campo.upper())
                info_doc["arquivo_renomeado"] = novo_caminho.split(os.sep)[-1]  # Apenas o nome do arquivo
            documentos_processados.append(info_doc)
    dados_aggregados["ocupacao"] = form_data.get("ocupacao")
    if "contribuicoes" in dados_aggregados:
        beneficio = calcular_beneficio(dados_aggregados["contribuicoes"])
        dados_aggregados["beneficio"] = beneficio
    else:
        dados_aggregados["beneficio"] = None
    dados_aggregados["documentos"] = documentos_processados

    nomes = [doc.get("nome", "").lower() for doc in documentos_processados if doc.get("nome") and doc.get("nome") != "Não encontrado"]
    if nomes and len(set(nomes)) > 1:
        dados_aggregados["inconsistencia_nomes"] = "Inconsistência nos nomes extraídos. Verifique manualmente."
    else:
        dados_aggregados["inconsistencia_nomes"] = "OK"

    return gerar_relatorio(dados_aggregados)

# ========================================
# Função para Gerar PDF a partir de HTML
# ========================================
def gerar_pdf_report(html_content):
    result = io.BytesIO()
    pisa_status = pisa.CreatePDF(html_content, dest=result)
    if pisa_status.err:
        return None
    result.seek(0)
    return result

# ========================================
# Rota para Download dos Arquivos
# ========================================
@app.route("/download/<path:filename>")
def download_file(filename):
    return send_from_directory("temp", filename, as_attachment=True)

# ========================================
# Endpoint API: Upload (JSON)
# ========================================
@app.route("/upload", methods=["POST"])
def api_upload():
    form_files = request.files
    form_data = request.form
    relatorio = processar_documentos(form_files, form_data)
    return jsonify(relatorio)

# ========================================
# Interface Web: Página Inicial (Landing Page)
# ========================================
@app.route("/")
def index():
    logo_url = "https://i.ibb.co/s9jZVY0v/Mutua-Logo-2-finalizada-quadrada.png"
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <title>Mútua - Assessoria Maternidade | VAII</title>
      <style>
        body {{
          margin: 0;
          padding: 0;
          font-family: Arial, sans-serif;
          background: linear-gradient(135deg, #F9DEE3 0%, #A8BCD0 100%);
        }}
        .header {{
          text-align: center;
          padding: 40px 20px;
          color: #333;
        }}
        .header img {{
          width: 120px;
          border-radius: 50%;
        }}
        .header h1 {{
          margin: 20px 0 10px;
          font-size: 28px;
        }}
        .header p {{
          margin: 5px 0;
          font-size: 18px;
          color: #555;
        }}
        .content {{
          max-width: 800px;
          margin: 30px auto;
          background: #fff;
          padding: 30px;
          border-radius: 10px;
          box-shadow: 0 2px 5px rgba(0,0,0,0.1);
          text-align: center;
        }}
        .content h2 {{
          color: #007BFF;
        }}
        .content p {{
          font-size: 16px;
          line-height: 1.5;
          color: #555;
        }}
        .button {{
          display: inline-block;
          padding: 12px 20px;
          background: #28a745;
          color: #fff;
          text-decoration: none;
          border-radius: 5px;
          font-size: 18px;
          margin-top: 20px;
        }}
        .button:hover {{
          background: #218838;
        }}
        .footer {{
          text-align: center;
          padding: 20px;
          font-size: 14px;
          color: #777;
        }}
      </style>
    </head>
    <body>
      <div class="header">
        <img src="{logo_url}" alt="Logo Mútua - Assessoria Maternidade">
        <h1>Mútua - Assessoria Maternidade</h1>
        <p>Verificador Automático de Informações para INSS (VAII)</p>
      </div>
      <div class="content">
        <h2>Bem-vindo ao nosso sistema!</h2>
        <p>Este é o VAII, um sistema desenvolvido pela Mútua - Assessoria Maternidade para pré-processar e analisar documentos destinados à solicitação de benefícios do INSS, como o auxílio-maternidade.</p>
        <p>O VAII extrai informações dos documentos enviados – incluindo certidões de nascimento, RGs, CPFs, extrato do CNIS e outros – valida os dados, identifica inconsistências e calcula um valor médio estimado do benefício.</p>
        <p>Além disso, o sistema verifica se todos os documentos obrigatórios estão presentes e alerta se algum estiver faltando ou se a qualidade do arquivo for baixa. Os relatórios gerados são para uso interno, ajudando nossos vendedores a preparar solicitações manualmente para o INSS.</p>
        <a class="button" href="/upload_form">Fazer Upload dos Documentos</a>
      </div>
      <div class="footer">
        © 2025 Mútua - Assessoria Maternidade. Todos os direitos reservados.<br>
        O VAII é uma ferramenta interna para pré-processamento e não possui integração com sistemas do INSS. Todos os envios ao INSS devem ser feitos manualmente pelos canais oficiais.
      </div>
    </body>
    </html>
    """
    return render_template_string(html)

# ========================================
# Interface Web: Formulário de Upload Segmentado com Loading
# ========================================
@app.route("/upload_form", methods=["GET"])
def upload_form_view():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <title>Upload de Documentos - VAII</title>
      <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f0f4f8; margin: 0; padding: 0; }
        .upload-container { max-width: 700px; margin: 50px auto; background: #fff; padding: 40px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
        h1 { text-align: center; color: #007BFF; margin-bottom: 30px; }
        label { font-weight: bold; margin-top: 20px; display: block; color: #333; }
        input[type="file"], select { width: 100%; padding: 10px; margin-top: 8px; border: 1px solid #ccc; border-radius: 4px; background: #fafafa; }
        input[type="submit"] { margin-top: 30px; width: 100%; padding: 12px; background: #28a745; border: none; border-radius: 4px; color: #fff; font-size: 18px; cursor: pointer; transition: background 0.3s ease; }
        input[type="submit"]:hover { background: #218838; }
        .loading { display: none; text-align: center; font-size: 18px; color: #555; margin-top: 20px; }
      </style>
      <script>
        function enviarFormulario(event) {
          event.preventDefault();
          document.getElementById('formUpload').style.display = 'none';
          document.getElementById('loading').style.display = 'block';
          event.target.submit();
        }
        function atualizarCampos() {
          var ocupacao = document.getElementById("ocupacao").value;
          if (ocupacao === "MEI") {
            document.getElementById("campo_mei").style.display = "block";
          } else {
            document.getElementById("campo_mei").style.display = "none";
          }
        }
      </script>
    </head>
    <body>
      <div class="upload-container">
        <h1>Upload de Documentos</h1>
        <form id="formUpload" method="POST" action="/upload_form" enctype="multipart/form-data" onsubmit="enviarFormulario(event)">
          <label for="rg">RG da Mãe:</label>
          <input type="file" name="rg" accept=".pdf" required>

          <label for="certidao">Certidão de Nascimento do Filho:</label>
          <input type="file" name="certidao" accept=".pdf" required>

          <label for="cnis">Extrato/CNIS:</label>
          <input type="file" name="cnis" accept=".pdf" required>

          <label for="pis">PIS/PASEP:</label>
          <input type="file" name="pis" accept=".pdf" required>

          <label for="ocupacao">Selecione a Ocupação da Mãe:</label>
          <select name="ocupacao" id="ocupacao" onchange="atualizarCampos()" required>
            <option value="CLT">CLT</option>
            <option value="MEI">MEI</option>
            <option value="Autônoma">Autônoma</option>
            <option value="Desempregada">Desempregada</option>
          </select>

          <div id="campo_mei" style="display:none;">
            <label for="mei">Comprovante MEI:</label>
            <input type="file" name="mei" accept=".pdf">
          </div>

          <input type="submit" value="Enviar">
        </form>
        <div id="loading" class="loading">
          <p>Carregando... Por favor aguarde enquanto processamos os documentos.</p>
        </div>
      </div>
    </body>
    </html>
    """
    return render_template_string(html)

# ========================================
# Interface Web: Relatório e Opção de Baixar PDF/CSV
# ========================================
@app.route("/upload_form", methods=["POST"])
def upload_form_post():
    form_files = request.files
    form_data = request.form
    ocupacao = form_data.get("ocupacao")
    relatorio = processar_documentos(form_files, form_data)
    relatorio["aprovacao"] = calcular_aprovacao(relatorio, ocupacao)
    relatorio["ocupacao"] = ocupacao

    # Renderizar o HTML do relatório com espaçamento mais compacto
    report_html = render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <title>Relatório de Análise - VAII</title>
      <style>
        body { font-family: Arial, sans-serif; background-color: #f8f9fa; margin: 0; padding: 10px; }
        .container { background: #fff; max-width: 800px; margin: 10px auto; padding: 20px; border-radius: 5px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        h1 { text-align: center; font-size: 24px; color: #333; margin-bottom: 10px; }
        .section { margin-bottom: 10px; }
        .section h2 { border-bottom: 1px solid #007BFF; padding-bottom: 3px; color: #007BFF; font-size: 18px; }
        .data { font-size: 14px; margin: 5px 0; }
        .data span { font-weight: bold; }
        .mensagem { font-size: 16px; color: red; text-align: center; margin: 10px 0; }
        .incompleto { font-size: 14px; color: orange; margin: 5px 0; }
        a.button { display: inline-block; padding: 8px 12px; background: #28a745; color: #fff; text-decoration: none; border-radius: 4px; margin: 5px; font-size: 14px; }
        a.button:hover { background: #218838; }
      </style>
    </head>
    <body>
      <div class="container">
        <h1>Relatório de Análise</h1>
        <div class="section">
          <h2>Avaliação Final</h2>
          <div class="data"><span>Ocupação:</span> {{ dados.ocupacao }}</div>
          <div class="data"><span>Aprovação:</span> {{ dados.aprovacao }}</div>
        </div>
        {% if not dados.elegibilidade.pagou_12_meses %}
          <div class="mensagem">{{ dados.elegibilidade.mensagem }}</div>
        {% else %}
          <div class="section">
            <h2>Dados Básicos</h2>
            <div class="data"><span>Nome:</span> {{ dados.nome }}</div>
            <div class="data"><span>CPF:</span> {{ dados.cpf }}</div>
            <div class="data"><span>CPF Válido:</span> {{ 'Sim' if dados.cpf_valido else 'Não' }}</div>
            {% if dados.pis %}
            <div class="data"><span>PIS/PASEP:</span> {{ dados.pis }}</div>
            {% endif %}
            {% if dados.beneficio_estimado %}
            <div class="data"><span>Benefício Estimado:</span> R$ {{ dados.beneficio_estimado }}</div>
            {% endif %}
          </div>
        {% endif %}
        <div class="section">
          <h2>Elegibilidade</h2>
          <div class="data"><span>Pagou 12 meses:</span> {{ 'Sim' if dados.elegibilidade.pagou_12_meses else 'Não' }}</div>
          <div class="data"><span>Qualidade de Segurada:</span> {{ 'Sim' if dados.elegibilidade.qualidade_segurada else 'Não' }}</div>
          <div class="data"><span>Dentro do Prazo:</span> {{ 'Sim' if dados.elegibilidade.dentro_prazo else 'Não' }}</div>
          <div class="data"><span>Observação:</span> {{ dados.elegibilidade.mensagem }}</div>
        </div>
        {% if dados.informacoes_incompletas %}
        <div class="section">
          <h2>Documentos Faltantes</h2>
          <div class="incompleto">
            Faltam: {{ dados.informacoes_incompletas | join(", ") }}.
          </div>
        </div>
        {% endif %}
        <div class="section">
          <h2>Documentos Processados</h2>
          {% for doc in dados.documentos_processados %}
            <div class="data">
              <span>Tipo:</span> {{ doc.tipo }}<br>
              <span>Arquivo Original:</span> {{ doc.arquivo_original }}<br>
              <span>Arquivo Renomeado:</span> {{ doc.arquivo_renomeado if doc.arquivo_renomeado is defined else 'N/A' }}<br>
              <span>Qualidade do Texto:</span> {{ doc.qualidade }}<br>
              {% if doc.qualidade.startswith("OK") and doc.get("arquivo_renomeado") %}
                <a class="button" href="/download/{{ doc.arquivo_renomeado }}">Baixar</a>
              {% endif %}
            </div>
            <hr style="margin:5px 0;">
          {% endfor %}
        </div>
        <div style="text-align:center; margin-top:10px;">
          <a class="button" href="/upload_form">Novo Upload</a>
          <a class="button" href="/export_prisma_manual?report={{ dados.cpf }}">Exportar para Prisma</a>
        </div>
      </div>
    </body>
    </html>
    """, dados=relatorio)

    # Salvar o HTML do relatório em um arquivo temporário na pasta "reports"
    report_filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    report_path = os.path.join("reports", report_filename)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    relatorio["report_filename"] = report_filename
    return report_html

# ========================================
# Função para Gerar CSV para Vendedores (Manual para Prisma)
# ========================================
@app.route("/export_prisma_manual", methods=["GET"])
def export_prisma_manual():
    relatorio = request.args.get("report_data", "{}")
    data = {
        "Nome da Solicitante": relatorio.get("nome", "Não encontrado"),
        "CPF": relatorio.get("cpf", "Não encontrado"),
        "PIS/PASEP": relatorio.get("pis", "Não encontrado"),
        "Contribuições (últimos 12 meses)": relatorio.get("contribuicoes", {}),
        "Benefício Estimado": f"R$ {relatorio.get('beneficio_estimado', 0):.2f}" if relatorio.get("beneficio_estimado") else "Não calculado",
        "Elegibilidade": relatorio.get("elegibilidade", {}).get("mensagem", "Verificar manualmente"),
        "Documentos Faltantes": relatorio.get("informacoes_incompletas", []),
        "Ocupação": relatorio.get("ocupacao", "Não informada"),
        "Aprovação": relatorio.get("aprovacao", "Não avaliada")
    }
    output = io.StringIO()
    pd.DataFrame([data]).to_csv(output, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="dados_prisma.csv", mimetype="text/csv")

# ========================================
# Roda o Servidor
# ========================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
