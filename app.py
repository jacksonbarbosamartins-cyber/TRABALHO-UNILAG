import streamlit as st
import pandas as pd
import sqlite3
from io import BytesIO
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
import plotly.express as px
import json
import os

# ================== Configs ==================
st.set_page_config(page_title="Folha & DRE v7.13 (sistema contábil)", layout="wide")
DB = "folha_v7_13.db"
CONFIG_FILE = "config_v7_13.json"

# Theme (CSS) - visual estilo sistema contábil azul
st.markdown("""
<style>
:root {
    --primary: #0B5FFF;
    --secondary: #063A7A;
    --accent: #1E90FF;
    --bg1: #F3F7FF;
    --bg2: #E9F1FF;
}

body {
    background: var(--bg1);
}

.block-container {
    background-color: white;
    padding: 18px;
    border-radius: 12px;
    box-shadow: 0px 6px 20px rgba(9,30,66,0.08);
}

h1, h2, h3, h4 {
    color: var(--secondary);
}

.stButton>button {
    background: linear-gradient(90deg,var(--primary),var(--accent));
    color: white;
    border-radius: 8px;
}

.stTabs [data-baseweb="tab"] {
    background-color: var(--bg2);
    border-radius: 8px;
    padding: 8px;
}

.stTabs [aria-selected="true"] {
    background-color: var(--primary) !important;
    color: white !important;
}

[data-testid="stMetric"] .stMetric-label, [data-testid="stMetric"] .stMetric-value {
    color: var(--secondary) !important;
}

</style>
""", unsafe_allow_html=True)

# Load/save config (CBS/IBS rates + accounting adjustment)

def load_config():
    default = {"CBS_RATE": 0.12, "IBS_RATE": 0.08, "ACCOUNTING_ADJ": 0.02}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                default.update(cfg)
        except Exception:
            pass
    return default


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass

config = load_config()

# Sidebar controls for tax rates (user-adjustable)
st.sidebar.header("Configurações (simuladas)")
CBS_RATE = st.sidebar.number_input("CBS - alíquota (%)", min_value=0.0, max_value=100.0, value=float(config.get("CBS_RATE",0.12))*100.0, step=0.1)
IBS_RATE = st.sidebar.number_input("IBS - alíquota (%)", min_value=0.0, max_value=100.0, value=float(config.get("IBS_RATE",0.08))*100.0, step=0.1)
ACCOUNTING_ADJ = st.sidebar.number_input("Ajuste contábil - porcentagem (%)", min_value=-100.0, max_value=500.0, value=float(config.get("ACCOUNTING_ADJ",0.02))*100.0, step=0.1)
if st.sidebar.button("Salvar configurações"):
    cfg = {"CBS_RATE": CBS_RATE/100.0, "IBS_RATE": IBS_RATE/100.0, "ACCOUNTING_ADJ": ACCOUNTING_ADJ/100.0}
    save_config(cfg)
    st.sidebar.success("Configurações salvas no arquivo de configuração")

# Normalize to fractional rates used in cálculos
CBS_RATE = CBS_RATE/100.0
IBS_RATE = IBS_RATE/100.0
ACCOUNTING_ADJ = ACCOUNTING_ADJ/100.0

# ======= CONFIGURAÇÕES DE IMPOSTOS (ATUALIZADAS) =======
# INSS - faixas progressivas (valores consultados nas fontes oficiais)
INSS_BRACKETS = [
    (1518.00, 0.075),
    (2793.88, 0.09),
    (4190.83, 0.12),
    (8157.41, 0.14),
]
# IRRF - tabela mensal (base, inclusive limites, alíquota e parcela a deduzir)
IR_TABLE = [
    (0.00, 2428.80, 0.0, 0.0),
    (2428.81, 2826.65, 0.075, 182.16),
    (2826.66, 3751.05, 0.15, 394.16),
    (3751.06, 4664.68, 0.225, 675.49),
    (4664.69, float('inf'), 0.275, 908.73),
]
DEPENDENT_DEDUCTION = 189.59

# ======= UTILITÁRIOS E BANCO =======

def get_conn():
    return sqlite3.connect(DB, check_same_thread=False)


def init_db():
    conn = get_conn(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS employees (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, cpf TEXT, role TEXT,
                 admission TEXT, salary_bruto REAL, dependents INTEGER DEFAULT 0, benefits REAL DEFAULT 0, other_deductions REAL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS services (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT, value REAL, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS products (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT, quantity INTEGER DEFAULT 0, unit_value REAL DEFAULT 0, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sales (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ref_id INTEGER, qty INTEGER, total REAL, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS costs (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT, amount REAL, kind TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS service_costs (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, service_id INTEGER, cost_id INTEGER, portion REAL, created_at TEXT)""")
    conn.commit(); conn.close()

init_db()

# ======= CÁLCULOS =======

def calc_inss(salary):
    total = 0.0
    prev = 0.0
    details = []
    for limit, rate in INSS_BRACKETS:
        if salary > prev:
            taxable = min(limit - prev, max(0.0, salary - prev))
            amount = round(taxable * rate, 2)
            details.append({"from": prev, "to": limit, "rate": rate, "taxable": taxable, "amount": amount})
            total += amount
            prev = limit
        else:
            break
    return round(total, 2), details


def calc_irrf(salary, inss, other_deductions, dependents):
    base = salary - inss - other_deductions - dependents * DEPENDENT_DEDUCTION
    base = round(max(base, 0.0), 2)
    for low, high, rate, parcela in IR_TABLE:
        if low <= base <= high:
            ir = round(max(base * rate - parcela, 0.0), 2)
            return ir, rate, parcela, base
    return 0.0, 0.0, 0.0, base


def money(v):
    try:
        return f"R$ {v:,.2f}".replace(',', 'TEMP').replace('.', ',').replace('TEMP', '.')
    except:
        return "R$ 0,00"


def generate_holerite_pdf(emp, extra_items=None):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=12*mm,leftMargin=12*mm,topMargin=12*mm,bottomMargin=12*mm)
    elems = []
    elems.append(Paragraph("Holerite - Folha de Pagamento", ParagraphStyle('title', fontSize=14, alignment=1, textColor=colors.HexColor("#0B5FFF"))))
    elems.append(Spacer(1,6))
    info = [["Funcionário:", emp.get('name',''), "CPF:", emp.get('cpf','') or ""],
            ["Cargo:", emp.get('role','') or "", "Admissão:", emp.get('admission','') or ""]]
    elems.append(Table(info, colWidths=[40*mm,70*mm,25*mm,40*mm]))
    elems.append(Spacer(1,8))

    salary = float(emp.get('salary_bruto') or 0.0)
    other = float(emp.get('other_deductions') or 0.0)
    dependents = int(emp.get('dependents') or 0)
    benefits = float(emp.get('benefits') or 0.0)
    total_prov = round(salary + benefits,2)
    inss_total, inss_details = calc_inss(salary)
    ir_total, ir_rate, ir_parcela, base_ir = calc_irrf(salary, inss_total, other, dependents)
    fgts = round(salary * 0.08,2)
    liquido = round(total_prov - (inss_total + ir_total + other),2)

    rows = [["Descrição","Proventos","Descontos"],
            ["Salário Base", money(salary), ""],
            ["Benefícios", money(benefits), ""],
            ["", "",""]]
    rows += [["INSS - Detalhamento","",""]]
    for d in inss_details:
        rows += [[f"Faixa {d['from']:.2f} - {d['to']:.2f} ({d['rate']*100:.0f}%)","", money(d['amount'])]]
    rows += [["Total INSS","", money(inss_total)]]
    rows += [["Base IR (salário - INSS - dependentes - outras)", money(base_ir), ""]]
    rows += [[f"IRRF ({int(ir_rate*100)}%)","", money(ir_total)]]
    rows += [["Parcela a deduzir (IR)", "", money(ir_parcela)]]
    rows += [["Outras Deduções","", money(other)]]
    rows += [["FGTS (8%) - informativo", money(fgts), ""]]
    rows += [["","", ""], ["Total Bruto", money(total_prov), ""], ["Total Líquido", money(liquido), ""]]

    t = Table(rows, colWidths=[90*mm,45*mm,45*mm])
    style = TableStyle([("BACKGROUND",(0,0),(2,0),colors.HexColor("#0B5FFF")),("TEXTCOLOR",(0,0),(2,0),colors.white),
                       ("ALIGN",(1,1),(-1,-1),"RIGHT"),("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#DDDDDD"))])
    t.setStyle(style)
    elems.append(t)
    doc.build(elems)
    buffer.seek(0)
    return buffer


def generate_dre_pdf(dre):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=12*mm,leftMargin=12*mm,topMargin=12*mm,bottomMargin=12*mm)
    elems = []
    elems.append(Paragraph("DRE - Demonstração do Resultado do Exercício", ParagraphStyle('title', fontSize=14, alignment=1, textColor=colors.HexColor("#063A7A"))))
    elems.append(Spacer(1,6))
    rows = [[Paragraph("Item", ParagraphStyle('n',fontSize=9)), Paragraph("Valor", ParagraphStyle('n',fontSize=9))]]
    for k,v in dre.items():
        rows.append([Paragraph(k, ParagraphStyle('n',fontSize=9)), Paragraph(money(v), ParagraphStyle('n',fontSize=9, alignment=2))])
    t = Table(rows, colWidths=[120*mm,50*mm])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#DDDDDD"))]))
    elems.append(t)
    doc.build(elems)
    buffer.seek(0)
    return buffer

# ================== UI ==================
st.title("Folha & DRE - v7.13 (sistema contábil) - Tema: Azul Contábil")
col1, col2 = st.columns([3,1])
with col2:
    st.markdown("""#### Painel""")
    df_emp = pd.read_sql_query("SELECT * FROM employees", get_conn())
    df_serv = pd.read_sql_query("SELECT * FROM services", get_conn())
    df_prod = pd.read_sql_query("SELECT * FROM products", get_conn())
    st.metric("Funcionários", len(df_emp))
    st.metric("Serviços", len(df_serv))
    st.metric("Produtos", len(df_prod))

tabs = st.tabs(["📑 Funcionários","🧾 Folha","💼 Serviços","🛒 Produtos/Vendas","⚙ Custos","🛠 Custos/Serviços","📘 DRE","📊 Indicadores","📖 Direitos Trabalhistas (Expandido)"])

with tabs[0]:
    st.header("Funcionários - cadastro")
    with st.form("f_emp", clear_on_submit=True):
        name = st.text_input("Nome", key="emp_name")
        cpf = st.text_input("CPF", key="emp_cpf")
        role = st.text_input("Cargo", key="emp_role")
        admission = st.date_input("Data de admissão", key="emp_adm")
        salary = st.number_input("Salário Bruto (R$)", min_value=0.0, step=100.0, key="emp_salary")
        dependents = st.number_input("Dependentes", min_value=0, step=1, key="emp_deps")
        benefits = st.number_input("Benefícios (R$)", min_value=0.0, step=1.0, key="emp_ben")
        other = st.number_input("Outras deduções (R$)", min_value=0.0, step=1.0, key="emp_other")
        if st.form_submit_button("Salvar Funcionário"):
            conn = get_conn(); c = conn.cursor()
            c.execute("INSERT INTO employees (name, cpf, role, admission, salary_bruto, dependents, benefits, other_deductions) VALUES (?,?,?,?,?,?,?,?)",
                      (name, cpf, role, str(admission), salary, dependents, benefits, other))
            conn.commit(); conn.close(); st.success("Funcionário salvo.")

    df_emp = pd.read_sql_query("SELECT * FROM employees ORDER BY id DESC", get_conn())
    st.dataframe(df_emp)

with tabs[1]:
    st.header("Folha de Pagamento")
    df_emp = pd.read_sql_query("SELECT * FROM employees", get_conn())
    if df_emp.empty:
        st.info("Cadastre funcionários na aba Funcionários.")
    else:
        ids = df_emp["id"].tolist()
        sel_index = 0
        if 'holerite_selected' in st.session_state:
            try:
                sel_index = ids.index(st.session_state['holerite_selected'])
            except Exception:
                sel_index = 0
        if not isinstance(sel_index, int) or sel_index<0 or sel_index>=len(ids):
            sel_index = 0
        sel = st.selectbox("Selecione funcionário (ID)", ids, index=sel_index, key="pay_sel")
        emp = df_emp[df_emp["id"]==sel].iloc[0].to_dict()
        st.write("Funcionário:", emp.get("name"))
        with st.form("f_payroll"):
            extra = st.number_input("Horas extras (R$)", min_value=0.0, step=1.0, key="pay_extra")
            dsr = st.number_input("DSR (R$)", min_value=0.0, step=1.0, key="pay_dsr")
            decimo = st.number_input("13º proporcional (R$)", min_value=0.0, step=1.0, key="pay_13th")
            ferias = st.number_input("Férias proporcionais (R$)", min_value=0.0, step=1.0, key="pay_vac")
            other = st.number_input("Outras deduções (R$)", value=float(emp.get("other_deductions") or 0.0), key="pay_other")
            if st.form_submit_button("Calcular & Gerar Holerite"):
                conn = get_conn(); c = conn.cursor()
                c.execute("UPDATE employees SET other_deductions=?, benefits=? WHERE id=?",
                          (other, float(emp.get("benefits") or 0.0), int(sel)))
                conn.commit(); conn.close()
                emp = pd.read_sql_query(f"SELECT * FROM employees WHERE id={int(sel)}", get_conn()).iloc[0].to_dict()
                pdf = generate_holerite_pdf(emp)
                st.session_state['last_holerite'] = pdf.getvalue()
                st.success("Holerite gerado — use o botão abaixo para baixar (fora do form).")
        if 'last_holerite' in st.session_state:
            st.download_button("📥 Baixar Holerite (PDF)", data=st.session_state['last_holerite'],
                                file_name=f"holerite_{emp['name'].replace(' ','_')}.pdf", mime="application/pdf", key="dl_hol")

with tabs[2]:
    st.header("Serviços - receitas")
    with st.form("f_service", clear_on_submit=True):
        sdesc = st.text_input("Descrição do serviço", key="s_desc")
        svalue = st.number_input("Valor (R$)", min_value=0.0, step=1.0, key="s_val")
        if st.form_submit_button("Salvar Serviço"):
            conn = get_conn(); c = conn.cursor(); c.execute("INSERT INTO services (description, value, created_at) VALUES (?,?,?)",(sdesc, svalue, datetime.now().strftime('%Y-%m-%d'))); conn.commit(); conn.close(); st.success("Serviço salvo.")
    df_serv = pd.read_sql_query("SELECT * FROM services ORDER BY id DESC", get_conn())
    st.dataframe(df_serv)

with tabs[3]:
    st.header("Produtos e Vendas")
    with st.form("f_prod", clear_on_submit=True):
        pdesc = st.text_input("Descrição do produto", key="p_desc")
        pqty = st.number_input("Quantidade em estoque (opcional)", min_value=0, step=1, key="p_qty")
        punit = st.number_input("Valor unitário (R$)", min_value=0.0, step=0.01, key="p_unit")
        if st.form_submit_button("Salvar Produto"):
            conn = get_conn(); c = conn.cursor(); c.execute("INSERT INTO products (description, quantity, unit_value, created_at) VALUES (?,?,?,?)",(pdesc,int(pqty),float(punit), datetime.now().strftime('%Y-%m-%d'))); conn.commit(); conn.close(); st.success("Produto salvo.")
    df_prod = pd.read_sql_query("SELECT * FROM products ORDER BY id DESC", get_conn())
    st.dataframe(df_prod)
    st.markdown("---")
    st.subheader("Registrar venda")
    df_prod2 = pd.read_sql_query("SELECT * FROM products", get_conn())
    df_serv2 = pd.read_sql_query("SELECT * FROM services", get_conn())
    kind = st.selectbox("Tipo", ["Produto","Serviço"], key="sale_kind")
    if kind=="Produto" and not df_prod2.empty:
        selp = st.selectbox("Selecionar produto", df_prod2['description'].tolist(), key="sale_prod")
        qty = st.number_input("Quantidade", min_value=1, step=1, key="sale_qty")
        price = float(df_prod2[df_prod2['description']==selp].iloc[0]['unit_value'])
        if st.button("Registrar Venda Produto"):
            total = qty * price
            conn = get_conn(); c = conn.cursor(); c.execute("INSERT INTO sales (kind, ref_id, qty, total, created_at) VALUES (?,?,?,?,?)",(kind, int(df_prod2[df_prod2['description']==selp].iloc[0]['id']), int(qty), float(total), datetime.now().strftime('%Y-%m-%d'))); conn.commit(); conn.close(); st.success("Venda cadastrada.")
    if kind=="Serviço" and not df_serv2.empty:
        sels = st.selectbox("Selecionar serviço", df_serv2['description'].tolist(), key="sale_srv")
        price = float(df_serv2[df_serv2['description']==sels].iloc[0]['value'])
        if st.button("Registrar Venda Serviço"):
            conn = get_conn(); c = conn.cursor(); c.execute("INSERT INTO sales (kind, ref_id, qty, total, created_at) VALUES (?,?,?,?,?)",(kind, int(df_serv2[df_serv2['description']==sels].iloc[0]['id']), 1, float(price), datetime.now().strftime('%Y-%m-%d'))); conn.commit(); conn.close(); st.success("Venda cadastrada.")
    st.dataframe(pd.read_sql_query("SELECT * FROM sales ORDER BY id DESC", get_conn()))

with tabs[4]:
    st.header("Custos - fixos e variáveis")
    with st.form("f_cost", clear_on_submit=True):
        cdesc = st.text_input("Descrição", key="cost_desc")
        camt = st.number_input("Valor (R$)", min_value=0.0, step=0.01, key="cost_amt")
        ckind = st.selectbox("Tipo", ["Direct","Indirect"], key="cost_kind")
        if st.form_submit_button("Salvar Custo"):
            conn = get_conn(); c = conn.cursor(); c.execute("INSERT INTO costs (description, amount, kind, created_at) VALUES (?,?,?,?)",(cdesc, float(camt), ckind, datetime.now().strftime('%Y-%m-%d'))); conn.commit(); conn.close(); st.success("Custo salvo.")
    df_cost = pd.read_sql_query("SELECT * FROM costs ORDER BY id DESC", get_conn())
    st.dataframe(df_cost)

with tabs[5]:
    st.header("Custos vinculados a Serviços (rateio)")
    df_serv = pd.read_sql_query("SELECT * FROM services", get_conn())
    df_cost = pd.read_sql_query("SELECT * FROM costs", get_conn())
    if df_serv.empty:
        st.info("Cadastre serviços antes de vincular custos.")
    else:
        sel_srv = st.selectbox("Selecionar serviço", df_serv['description'].tolist(), key="rate_srv")
        srv_id = int(df_serv[df_serv['description']==sel_srv].iloc[0]['id'])
        st.subheader("Vincular custo existente ao serviço")
        if not df_cost.empty:
            sel_cost = st.selectbox("Selecionar custo", df_cost['description'].tolist(), key="rate_cost")
            cost_id = int(df_cost[df_cost['description']==sel_cost].iloc[0]['id'])
            portion = st.number_input("Porção do custo para esse serviço (R$)", min_value=0.0, step=0.01, key="rate_portion")
            if st.button("Vincular custo ao serviço", key="rate_btn"):
                conn = get_conn(); c = conn.cursor(); c.execute("INSERT INTO service_costs (service_id, cost_id, portion, created_at) VALUES (?,?,?,?)",(srv_id, cost_id, float(portion), datetime.now().strftime('%Y-%m-%d'))); conn.commit(); conn.close(); st.success("Custo vinculado ao serviço.")
        df_link = pd.read_sql_query(f"SELECT sc.id, s.description as service, c.description as cost, sc.portion, sc.created_at FROM service_costs sc JOIN services s ON sc.service_id=s.id JOIN costs c ON sc.cost_id=c.id WHERE sc.service_id={srv_id} ORDER BY sc.id DESC", get_conn())
        st.dataframe(df_link)
        total_service_cost = float(df_link['portion'].sum()) if not df_link.empty else 0.0
        st.metric("Custo Total deste Serviço", money(total_service_cost))

with tabs[6]:
    st.header("DRE - Demonstração do Resultado do Exercício")
    df_sales = pd.read_sql_query("SELECT * FROM sales", get_conn())
    df_costs = pd.read_sql_query("SELECT * FROM costs", get_conn())
    receita_produtos = float(df_sales[df_sales['kind']=='Produto']['total'].sum()) if not df_sales.empty else 0.0
    receita_servicos = float(df_sales[df_sales['kind']=='Serviço']['total'].sum()) if not df_sales.empty else 0.0
    receita_bruta = receita_produtos + receita_servicos
    # aplica CBS e IBS sobre receita bruta usando as alíquotas configuráveis
    cbs = round(receita_bruta * CBS_RATE, 2)
    ibs = round(receita_bruta * IBS_RATE, 2)
    deducoes = cbs + ibs
    cpv = float(df_costs[df_costs['kind']=='Direct']['amount'].sum()) if not df_costs.empty else 0.0
    despesas = float(df_costs[df_costs['kind']=='Indirect']['amount'].sum()) if not df_costs.empty else 0.0
    folha = float(pd.read_sql_query("SELECT IFNULL(SUM(salary_bruto+IFNULL(benefits,0)),0) as f FROM employees", get_conn()).iloc[0]['f'])
    receita_liquida = receita_bruta - deducoes
    lucro_bruto = receita_liquida - cpv
    resultado_operacional = lucro_bruto - (despesas + folha)
    antes_ir = resultado_operacional
    ir_csll = 0.0
    lucro_liquido = antes_ir - ir_csll

    # Ajuste contábil (valor contabil alterado) aplicado sobre custos totais para demonstrar efeito contábil
    total_custos = custos = float(df_costs['amount'].sum()) if not df_costs.empty else 0.0
    adjusted_custos = round(total_custos * (1 + ACCOUNTING_ADJ), 2)

    dre_dict = {
        "Receita Bruta": receita_bruta,
        "(-) CBS (simulado)": cbs,
        "(-) IBS (simulado)": ibs,
        "Receita Líquida": receita_liquida,
        "(-) CPV": cpv,
        "Lucro Bruto": lucro_bruto,
        "(-) Despesas Operacionais (inclui folha)": (despesas + folha),
        "Resultado Operacional": resultado_operacional,
        "Total Custos (contábil)": total_custos,
        "Total Custos (ajustado)": adjusted_custos,
        "Lucro Líquido": lucro_liquido
    }
    st.dataframe(pd.DataFrame(list(dre_dict.items()), columns=["Item","Valor"]).style.format({"Valor":"R$ {:.2f}"}))
    if st.button("Gerar DRE (PDF)"):
        pdf = generate_dre_pdf(dre_dict); st.session_state['last_dre'] = pdf.getvalue(); st.success("DRE gerada.")
    if 'last_dre' in st.session_state:
        st.download_button("📥 Baixar DRE (PDF)", data=st.session_state['last_dre'], file_name="DRE_v7_13_atualizada.pdf", mime="application/pdf")

with tabs[7]:
    st.header("Indicadores Financeiros")
    df_sales = pd.read_sql_query("SELECT * FROM sales", get_conn())
    df_costs = pd.read_sql_query("SELECT * FROM costs", get_conn())
    receita = float(df_sales['total'].sum()) if not df_sales.empty else 0.0
    custos = float(df_costs['amount'].sum()) if not df_costs.empty else 0.0
    impostos = round(receita * (CBS_RATE + IBS_RATE), 2)
    lucro = receita - custos - impostos
    margem = (lucro/receita*100) if receita>0 else 0.0
    col1, col2, col3 = st.columns(3)
    col1.metric("Receita", money(receita)); col2.metric("Custos", money(custos)); col3.metric("Lucro (após CBS+IBS)", money(lucro))
    st.write(f"Margem de Lucro: {margem:.2f}%")
    if not df_sales.empty:
        fig = px.bar(df_sales, x='created_at', y='total', color='kind', title='Receitas por período')
        st.plotly_chart(fig, use_container_width=True)

with tabs[8]:
    st.header("📖 Direitos Trabalhistas – Guia Expandido e Explicações Contábeis")

    st.markdown("""
## 🧾 1) INSS – Contribuição Previdenciária
O **INSS (Instituto Nacional do Seguro Social)** é responsável pela aposentadoria, auxílio-doença, salário-maternidade, pensão por morte e outros benefícios.

### Como funciona:
- A contribuição do empregado é **progressiva**, por faixas.
- A empresa recolhe **20% sobre a folha** (regime geral), salvo desoneração.
- Também existem contribuições específicas (RAT/SAT, terceiros, FPAS).

### Importância contábil:
- Impacta diretamente no **custo de mão de obra**.
- Para o contador, entra como **encargo trabalhista obrigatório**.
- Deve ser registrado como **passivo a recolher** até o pagamento na GPS/DARF.

### Exemplos de benefícios garantidos:
- Aposentadoria (idade, tempo, invalidez)
- Auxílio-doença
- Auxílio-acidente
- Salário-maternidade
- Pensão por morte

---

## 🏛️ 2) Previdência Social – Explicação Contábil e Jurídica
A **Previdência Social** é um seguro público obrigatório que protege o trabalhador.

### Estrutura:
- **RGPS** – Regime Geral (INSS)
- **RPPS** – Servidores públicos
- **Complementar** – Fundos privados (opcional)

### Relevância contábil:
- Para empresas, é um dos principais **custos indiretos do trabalho**.
- Para funcionários, influencia no **salário líquido** e no cálculo do **IRRF**.
- Deve ser demonstrada em documentos como:
  - Folha de Pagamento
  - Holerite
  - E-social
  - DCTFWeb

---

## 🧮 3) Direito Tributário – Visão para a Área Contábil
O **Direito Tributário** regula a cobrança de tributos pela União, Estados e Municípios.

### Principais tributos no contexto empresarial:
- **CBS e IBS** (nova reforma tributária – tributos sobre consumo)
- **IRPJ** e **CSLL**
- **PIS/COFINS**
- **ISS**
- **ICMS**
- **IPI**

### Importância contábil:
- Determina a forma de apuração da empresa:
  - **Simples Nacional**
  - **Lucro Presumido**
  - **Lucro Real**
- Impacta diretamente no:
  - preço de venda
  - margem de lucro
  - DRE (Demonstração do Resultado)
  - fluxo de caixa
  - obrigações acessórias

---

## 📚 Links oficiais e confiáveis
Clique para abrir:

- [Portal Gov.br - Trabalho e Emprego](https://www.gov.br/trabalho-e-emprego/pt-br)
- [Consolidação das Leis do Trabalho (CLT)](https://www.planalto.gov.br/ccivil_03/decreto-lei/del5452.htm)
- [Tribunal Superior do Trabalho (TST)](https://www.tst.jus.br)
- [INSS – Informações oficiais](https://www.gov.br/inss/pt-br/assuntos/contribuicao)
- [Receita Federal – IRPF e IRPJ](https://www.gov.br/receitafederal/pt-br)
- [Portal eSocial](https://www.gov.br/esocial/pt-br)

---

**Observação**: As tabelas de INSS, IRRF, alíquotas tributárias e obrigações trabalhistas podem alterar ao longo do ano. Sempre consulte as fontes oficiais acima.
    """, unsafe_allow_html=True)

    st.success("Resumo expandido aplicado com sucesso!")

# ===== FIM DO APLICATIVO =====


# Nota: salve este arquivo como `app_v7_13.py` e execute com `streamlit run app_v7_13.py`.
