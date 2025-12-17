import streamlit as st
import pandas as pd
import plotly.express as px
import pdfplumber
import re
import json
from datetime import datetime
from streamlit_gsheets import GSheetsConnection

# --- CONFIGURAÇÃO VISUAL ---
st.set_page_config(page_title="FinAuto - Cloud", page_icon="☁️", layout="wide")
COR_PRIMARIA = "#0ea5e9"

# --- 1. BANCO DE DADOS (VIA GOOGLE SHEETS) ---
# A conexão agora busca os dados na nuvem, não mais no arquivo local
def get_connection():
    return st.connection("gsheets", type=GSheetsConnection)

def carregar_dados():
    conn = get_connection()
    try:
        # Lê a planilha. ttl=0 garante que não pegue dados velhos do cache
        df = conn.read(worksheet="Dados", ttl=0)
        # Garante que as colunas existam se a planilha estiver vazia
        colunas_esperadas = ['Data', 'Descricao', 'Categoria', 'Valor', 'Tipo', 'Origem', 'Detalhes_JSON']
        if df.empty or len(df.columns) < len(colunas_esperadas):
            return pd.DataFrame(columns=colunas_esperadas)
        
        # Garante que colunas vazias não quebrem o código (fillna)
        return df.fillna("")
    except Exception:
        # Se der erro (ex: planilha nova), retorna vazio
        return pd.DataFrame(columns=['Data', 'Descricao', 'Categoria', 'Valor', 'Tipo', 'Origem', 'Detalhes_JSON'])

def salvar_dados(novo_df):
    conn = get_connection()
    df_antigo = carregar_dados()
    
    # Tratamento para garantir compatibilidade
    novo_df = novo_df.fillna("")
    
    df_combinado = pd.concat([df_antigo, novo_df])
    # Remove duplicatas exatas
    df_final = df_combinado.drop_duplicates(subset=['Data', 'Descricao', 'Valor', 'Tipo'], keep='first')
    
    # Atualiza o Google Sheets
    conn.update(worksheet="Dados", data=df_final)
    st.cache_data.clear() # Limpa memória para ver atualização na hora
    return df_final

def deletar_registro(index_para_remover):
    conn = get_connection()
    df = carregar_dados()
    
    # Remove pelo index
    df_final = df.drop(index_para_remover)
    
    conn.update(worksheet="Dados", data=df_final)
    st.cache_data.clear()

# --- 2. FERRAMENTAS DE TEXTO ---

def limpar_valor(valor_str):
    if not isinstance(valor_str, str): return valor_str
    try:
        limpo = re.sub(r'[^\d,]', '', valor_str) 
        limpo = limpo.replace(',', '.')
        return float(limpo)
    except:
        return 0.0

def extrair_data_vencimento(texto):
    match = re.search(r'(\d{2}/\d{2}/\d{4})', texto)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
        except: pass
    return datetime.now().strftime("%Y-%m-%d")

# --- 3. DETETIVES ESPECIALIZADOS ---

def processar_contracheque(texto, nome_arquivo):
    valor_liquido = 0.0
    match_valor = re.search(r'Líquido.*?([\d\.]+,\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if match_valor: valor_liquido = limpar_valor(match_valor.group(1))
    
    data_pag = extrair_data_vencimento(texto)
    match_data = re.search(r'Data do Pagamento:\s*(\d{2}/\d{2}/\d{4})', texto, re.IGNORECASE)
    if match_data:
        try:
            data_pag = datetime.strptime(match_data.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
        except: pass

    desc_extra = "Salário Mensal"
    if "13." in texto or "DECIMO" in texto: desc_extra = "13º Salário"

    return pd.DataFrame([{
        'Data': data_pag, 'Descricao': f'Contracheque - {desc_extra}', 'Categoria': 'Salário',
        'Valor': valor_liquido, 'Tipo': 'Receita', 'Origem': nome_arquivo, 'Detalhes_JSON': ''
    }])

def processar_fatura_xp(texto, pdf_obj):
    valor_total = 0.0
    match_total = re.search(r'(?:Pagamento total|Valor total devido).*?R?\$?\s*([\d\.]+,\d{2})', texto, re.IGNORECASE)
    if match_total: valor_total = limpar_valor(match_total.group(1))

    data_venc = extrair_data_vencimento(texto)
    itens = []
    padrao_item = re.compile(r'(\d{2}/\d{2}/\d{2})\s+(.+?)\s+(\d{1,3}(?:\.\d{3})*,\d{2})')
    for page in pdf_obj.pages:
        for linha in (page.extract_text() or "").split('\n'):
            if padrao_item.search(linha): itens.append(linha)

    return pd.DataFrame([{
        'Data': data_venc, 'Descricao': 'Fatura Cartão XP', 'Categoria': 'Fatura Cartão',
        'Valor': valor_total, 'Tipo': 'Despesa', 'Origem': 'Fatura XP',
        'Detalhes_JSON': json.dumps(itens)
    }])

def processar_boleto_cemig(texto, nome_arquivo):
    valor_encontrado = 0.0
    match_cemig = re.search(r'Valor a pagar.*?R\$\)?\s*([\d\.]+,\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if match_cemig:
        valor_encontrado = limpar_valor(match_cemig.group(1))
    else:
        todos_valores = re.findall(r'R\$\s*([\d\.]+,\d{2})', texto)
        valores_float = [limpar_valor(v) for v in todos_valores]
        if valores_float: valor_encontrado = max(valores_float)

    data_venc = extrair_data_vencimento(texto)
    return pd.DataFrame([{
        'Data': data_venc, 'Descricao': 'Conta de Luz (Cemig)', 'Categoria': 'Casa',
        'Valor': valor_encontrado, 'Tipo': 'Despesa', 'Origem': nome_arquivo, 'Detalhes_JSON': ''
    }])

def processar_generico(texto, nome_arquivo):
    chaves = [r'Valor do Documento', r'Valor Cobrado', r'Total a Pagar']
    valor_final = 0.0
    for chave in chaves:
        match = re.search(f'{chave}.*?R?\$?\s*([\d\.]+,\d{{2}})', texto, re.IGNORECASE | re.DOTALL)
        if match:
            v = limpar_valor(match.group(1))
            if v > valor_final: valor_final = v
            
    data_venc = extrair_data_vencimento(texto)
    empresa = "Boleto Diverso"
    cat = "Outros"
    
    if "blink" in texto.lower(): 
        empresa = "Internet (Blink)"
        cat = "Serviços"

    return pd.DataFrame([{
        'Data': data_venc, 'Descricao': empresa, 'Categoria': cat,
        'Valor': valor_final, 'Tipo': 'Despesa', 'Origem': nome_arquivo, 'Detalhes_JSON': ''
    }])

def roteador(pdf_file):
    try:
        with pdfplumber.open(pdf_file) as pdf:
            texto = "\n".join([p.extract_text() or "" for p in pdf.pages])
            if "CONTRACHEQUE" in texto or ("PREFEITURA" in texto and "Líquido" in texto):
                return processar_contracheque(texto, pdf_file.name)
            elif "XP" in texto and "Fatura" in texto:
                return processar_fatura_xp(texto, pdf)
            elif "CEMIG" in texto:
                return processar_boleto_cemig(texto, pdf_file.name)
            else:
                return processar_generico(texto, pdf_file.name)
    except Exception as e:
        return pd.DataFrame()

# --- 4. INTERFACE ---

st.markdown(f"<h1 style='color:{COR_PRIMARIA}'>FinAuto <small>Cloud</small></h1>", unsafe_allow_html=True)

if 'arquivos_processados' not in st.session_state:
    st.session_state.arquivos_processados = set()

tabs = st.tabs(["📊 Dashboard", "📥 Importar Documentos", "📝 Lançar Manual", "📂 Banco de Dados"])

# --- ABA 2: IMPORTAR ---
with tabs[1]:
    st.write("Envie PDFs (Salários, XP, Cemig, Blink).")
    arquivos = st.file_uploader("Arquivos", type=['pdf'], accept_multiple_files=True)
    
    if arquivos:
        df_preview = pd.DataFrame()
        novos_arquivos = [arq for arq in arquivos if arq.name not in st.session_state.arquivos_processados]
        
        if novos_arquivos:
            for arq in novos_arquivos:
                df_temp = roteador(arq)
                if not df_temp.empty:
                    df_preview = pd.concat([df_preview, df_temp])
                    st.success(f"✅ Lido: {arq.name} ({df_temp['Tipo'].iloc[0]}) - R$ {df_temp['Valor'].iloc[0]}")
                else:
                    st.error(f"❌ Erro ao ler: {arq.name}")
            
            if not df_preview.empty:
                st.info("Confira e Salve.")
                editor = st.data_editor(df_preview, num_rows="dynamic")
                if st.button("💾 Salvar Tudo no Google Sheets"):
                    salvar_dados(editor)
                    for arq in novos_arquivos: st.session_state.arquivos_processados.add(arq.name)
                    st.success("Salvo na Nuvem!")
                    st.balloons()
        elif len(arquivos) > 0:
            st.warning("Arquivos já processados. Recarregue a página (F5).")

# --- ABA 4: PLANILHA E FILTROS ---
with tabs[3]:
    st.markdown("### 📂 Gerenciamento (Google Sheets)")
    df = carregar_dados()
    
    if not df.empty:
        # Garante que Valor é numérico
        df['Valor'] = pd.to_numeric(df['Valor'], errors='coerce').fillna(0)
        
        c1, c2 = st.columns(2)
        cat_filtro = c1.multiselect("Categoria", options=df['Categoria'].unique())
        # Cria lista de meses ordenada
        df['Mes'] = df['Data'].apply(lambda x: str(x)[:7])
        meses = sorted(list(df['Mes'].unique()))
        mes_filtro = c2.selectbox("Mês", ["Todos"] + meses, index=len(meses))
        
        df_show = df.copy()
        if cat_filtro: df_show = df_show[df_show['Categoria'].isin(cat_filtro)]
        if mes_filtro != "Todos": df_show = df_show[df_show['Mes'] == mes_filtro]
        
        # Totais
        total_rec = df_show[df_show['Tipo'] == 'Receita']['Valor'].sum()
        total_desp = df_show[df_show['Tipo'] == 'Despesa']['Valor'].sum()
        saldo_periodo = total_rec - total_desp
        
        # Linha Total
        linha_total = pd.DataFrame([{
            'Data': '', 'Descricao': '--- SALDO DO PERÍODO ---', 'Categoria': '', 
            'Valor': saldo_periodo, 'Tipo': 'SALDO', 'Origem': '', 'Detalhes_JSON': ''
        }])
        df_visual = pd.concat([df_show.drop(columns=['Mes']), linha_total], ignore_index=True)

        st.dataframe(df_visual, use_container_width=True, column_config={"Valor": st.column_config.NumberColumn(format="R$ %.2f"), "Detalhes_JSON": None})
        
        m1, m2, m3 = st.columns(3)
        m1.info(f"Entradas: **R$ {total_rec:.2f}**")
        m2.error(f"Saídas: **R$ {total_desp:.2f}**")
        m3.success(f"Saldo: **R$ {saldo_periodo:.2f}**") if saldo_periodo >=0 else m3.warning(f"Saldo: **R$ {saldo_periodo:.2f}**")

        # Exclusão
        id_del = st.number_input("ID para excluir (índice da tabela)", min_value=0, step=1)
        if st.button("🗑️ Excluir Linha"):
            if id_del in df.index:
                deletar_registro(id_del)
                st.success("Deletado! (Pode demorar uns segundos para atualizar)")
                st.experimental_rerun()

# --- ABA 1: DASHBOARD ---
with tabs[0]:
    df = carregar_dados()
    if not df.empty:
        df['Valor'] = pd.to_numeric(df['Valor'], errors='coerce').fillna(0)
        total_desp = df[df['Tipo']=='Despesa']['Valor'].sum()
        total_rec = df[df['Tipo']=='Receita']['Valor'].sum()
        
        k1, k2, k3 = st.columns(3)
        k1.metric("Receitas", f"R$ {total_rec:.2f}")
        k2.metric("Despesas", f"R$ {total_desp:.2f}")
        k3.metric("Saldo Geral", f"R$ {total_rec - total_desp:.2f}")
        
        c1, c2 = st.columns(2)
        fig_pie = px.pie(df[df['Tipo']=='Despesa'], values='Valor', names='Categoria', title="Gastos")
        c1.plotly_chart(fig_pie, use_container_width=True)
        
        df_chart = df.copy()
        df_chart['Valor_Sinal'] = df_chart.apply(lambda x: x['Valor'] if x['Tipo'] == 'Receita' else -x['Valor'], axis=1)
        fig_bar = px.bar(df_chart, x='Data', y='Valor_Sinal', color='Tipo', title="Fluxo")
        c2.plotly_chart(fig_bar, use_container_width=True)
    else:
        st.info("Banco de dados vazio.")

# --- ABA 3: MANUAL ---
with tabs[2]:
    with st.form("manual"):
        d = st.date_input("Data")
        v = st.number_input("Valor", min_value=0.0)
        desc = st.text_input("Descrição")
        cat = st.selectbox("Categoria", ["Salário", "Casa", "Transporte", "Lazer", "Outros"])
        t = st.radio("Tipo", ["Despesa", "Receita"], horizontal=True)
        if st.form_submit_button("Salvar"):
            salvar_dados(pd.DataFrame([{'Data': d, 'Descricao': desc, 'Categoria': cat, 'Valor': v, 'Tipo': t, 'Origem': 'Manual', 'Detalhes_JSON': ''}]))
            st.success("Salvo!")
```

### Passo 3: Configurar a "Chave" do Google (A parte chata, mas necessária)

Para o seu código ter permissão de escrever na sua planilha, precisamos criar um "Robô" no Google. Siga com calma:

1.  **Crie uma Planilha:**
    * Vá no [Google Sheets](https://sheets.new) e crie uma planilha vazia.
    * Nomeie ela como `FinAutoDB` (ou o que preferir).
    * Renomeie a aba (lá embaixo) de "Página1" para **`Dados`**.

2.  **Habilitar o Acesso:**
    * Acesse o [Google Cloud Console](https://console.cloud.google.com/).
    * Crie um "Novo Projeto" (dê o nome de FinAuto).
    * Na barra de busca, procure por **"Google Sheets API"** e clique em **Ativar**.
    * Procure por **"Google Drive API"** e clique em **Ativar**.

3.  **Criar o Robô (Service Account):**
    * No menu lateral, vá em **"IAM e Admin"** > **"Contas de Serviço"** (Service Accounts).
    * Clique em "+ Criar Conta de Serviço". Dê um nome e clique em Continuar/Concluir.
    * Vai aparecer um email estranho na lista (algo como `finauto@finauto-123.iam.gserviceaccount.com`). **Copie esse email.**

4.  **Compartilhar a Planilha:**
    * Volte na sua planilha do Google Sheets.
    * Clique em "Compartilhar" (Share).
    * Cole o email do robô que você copiou e dê permissão de **Editor**. (Isso autoriza o robô a escrever lá).

5.  **Baixar a Chave (JSON):**
    * Volte no Google Cloud Console, na lista de contas de serviço.
    * Clique nos três pontinhos da conta que você criou > **Gerenciar Chaves**.
    * Clique em "Adicionar Chave" > "Criar nova chave" > Selecione **JSON**.
    * Um arquivo será baixado no seu computador.

### Passo 4: Conectar tudo (O Segredo)

Agora precisamos dizer ao Streamlit onde está essa chave.

1.  Na pasta do seu projeto (`ProjetoFinAuto`), crie uma pasta chamada `.streamlit` (com ponto na frente).
2.  Dentro dela, crie um arquivo de texto chamado `secrets.toml`.
3.  Abra esse arquivo e cole o conteúdo abaixo, substituindo pelos dados que estão no arquivo JSON que você baixou:

```toml
[connections.gsheets]
spreadsheet = "Cole aqui o Link da sua Planilha do Google"
type = "service_account"
project_id = "xxx"
private_key_id = "xxx"
private_key = "xxx"
client_email = "xxx"
client_id = "xxx"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "xxx"