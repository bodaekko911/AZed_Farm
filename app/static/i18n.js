/**
 * i18n.js — runtime English ⇄ Arabic translation layer for AZed ERP.
 *
 * Loaded automatically by theme-init.js on every page (no router changes
 * needed). How it works:
 *
 *   1. A dictionary maps the English UI strings (as they appear in the
 *      DOM) to Arabic.
 *   2. When the language is "ar", a TreeWalker translates every text node,
 *      plus placeholder / title / aria-label / button-value attributes.
 *      The original English is stored on the node itself, so switching
 *      back to English restores the exact original text.
 *   3. A MutationObserver re-translates content rendered later by page
 *      JavaScript (tables, modals, fetch results).
 *   4. <html dir="rtl" lang="ar"> is set, an Arabic font (Cairo) is
 *      injected, and a few RTL CSS fixes are applied.
 *
 * The language is persisted in localStorage ("appLang") and synced across
 * tabs. A toggle button (ع / EN) is injected next to the theme toggle.
 *
 * Extending coverage: any visible English string that has no dictionary
 * entry is collected at runtime. Open the console and run
 *     window.__i18n.missing()
 * to get a ready-to-paste list of strings you still need to translate.
 * Add them to the DICT below and redeploy — nothing else to change.
 */
(function () {
  "use strict";
  if (window.__i18n) return;

  var LANG_KEY = "appLang";

  /* ──────────────────────────────────────────────────────────────────
   * DICTIONARY  (English → Arabic)
   * Keys must match the visible text exactly (after trimming).
   * The lookup also auto-handles leading emoji/symbols and trailing
   * "*", ":", "…", "...", so "🖨 Print" and "Date *" resolve via
   * "Print" and "Date".
   * ────────────────────────────────────────────────────────────────── */
  var DICT = {
    /* ── Brand / chrome ── */
    "AZed ERP": "AZed ERP",
    "Habiba Organic Farm": "مزرعة حبيبة العضوية",
    "Signed in as": "مسجل الدخول باسم",
    "Sign out": "تسجيل الخروج",
    "Toggle color mode": "تبديل وضع الألوان",
    "Admin Only": "للمشرف فقط",
    "Admin": "مشرف",
    "Good morning,": "صباح الخير،",
    "Good afternoon,": "مساء الخير،",
    "Good evening,": "مساء الخير،",

    /* ── Home modules / nav ── */
    "Core Operations": "العمليات الأساسية",
    "People & Finance": "الموظفون والمالية",
    "Management": "الإدارة",
    "Tools": "الأدوات",
    "Operations": "العمليات",
    "Finance": "المالية",
    "Sales": "المبيعات",
    "Stock": "المخزون",
    "Catalog": "الكتالوج",
    "CRM": "العملاء",
    "Purchasing": "المشتريات",
    "Manufacturing": "التصنيع",
    "Analytics": "التحليلات",
    "Returns": "المرتجعات",
    "Sustainability": "الاستدامة",
    "Human Resources": "الموارد البشرية",
    "Point of Sale": "نقطة البيع",
    "Process sales, scan barcodes, print receipts": "تنفيذ المبيعات، مسح الباركود، طباعة الإيصالات",
    "Inventory": "المخزون",
    "Stock levels, movements, adjustments": "مستويات المخزون والحركات والتسويات",
    "Products": "المنتجات",
    "Catalog, pricing, SKUs, categories": "الكتالوج والأسعار وأكواد المنتجات والفئات",
    "Customers": "العملاء",
    "Customer list, invoice history, balances": "قائمة العملاء وسجل الفواتير والأرصدة",
    "Suppliers": "الموردون",
    "Supplier directory, balances, payments, purchases": "دليل الموردين والأرصدة والمدفوعات والمشتريات",
    "Farm Intake": "استلام المزرعة",
    "Farm suppliers, deliveries, and intake tracking": "موردو المزرعة والتوريدات وتتبع الاستلام",
    "Production": "الإنتاج",
    "Process raw materials, packaging runs, track loss": "معالجة الخامات وتشغيلات التعبئة وتتبع الفاقد",
    "Receive Products": "استلام المنتجات",
    "B2B Wholesale": "مبيعات الجملة",
    "HR & Payroll": "الموارد البشرية والرواتب",
    "Employees, attendance, salary runs": "الموظفون والحضور وصرف الرواتب",
    "Expenses": "المصروفات",
    "Accounting": "المحاسبة",
    "Ledger, journal entries, P&L report": "دفتر الأستاذ والقيود وتقرير الأرباح والخسائر",
    "Reports": "التقارير",
    "Retail Refunds": "مرتجعات التجزئة",
    "Process returns, restore stock, reverse accounting": "معالجة المرتجعات واسترجاع المخزون وعكس القيود",
    "Refunds": "المرتجعات",
    "Animals": "الحيوانات",
    "Animal groups and feed consumption tracking": "مجموعات الحيوانات وتتبع استهلاك الأعلاف",
    "Carbon Footprint": "البصمة الكربونية",
    "Import Data": "استيراد البيانات",
    "Import customers and products from Excel": "استيراد العملاء والمنتجات من إكسل",
    "Audit Log": "سجل المراجعة",
    "Roles, permissions, passwords, activity log": "الأدوار والصلاحيات وكلمات المرور وسجل النشاط",
    "Full history of every action taken across the system": "السجل الكامل لكل إجراء تم في النظام",
    "Users": "المستخدمون",
    "User Management": "إدارة المستخدمين",
    "Sales dashboard": "لوحة المبيعات",
    "Farm dashboard": "لوحة المزرعة",
    "Dashboard": "لوحة التحكم",
    "Change Password": "تغيير كلمة المرور",

    /* ── Generic table headers / fields ── */
    "Date": "التاريخ",
    "Date / Time": "التاريخ / الوقت",
    "Time": "الوقت",
    "Name": "الاسم",
    "Full Name": "الاسم الكامل",
    "Product": "المنتج",
    "Status": "الحالة",
    "Notes": "ملاحظات",
    "Note": "ملاحظة",
    "Note (optional)": "ملاحظة (اختياري)",
    "Notes (optional)": "ملاحظات (اختياري)",
    "Optional notes": "ملاحظات اختيارية",
    "Type": "النوع",
    "SKU": "كود المنتج",
    "Total": "الإجمالي",
    "Actions": "إجراءات",
    "Qty": "الكمية",
    "QTY": "الكمية",
    "Quantity": "الكمية",
    "Total Qty": "إجمالي الكمية",
    "Category": "الفئة",
    "Categories": "الفئات",
    "Unit": "الوحدة",
    "By": "بواسطة",
    "Amount": "المبلغ",
    "Amount (EGP)": "المبلغ (ج.م)",
    "Phone": "الهاتف",
    "Reason": "السبب",
    "Outstanding": "المستحق",
    "Invoices": "الفواتير",
    "Invoice #": "فاتورة رقم",
    "Client": "العميل",
    "Clients": "العملاء",
    "B2B Clients": "عملاء الجملة",
    "Paid": "مدفوع",
    "Unpaid": "غير مدفوع",
    "Partial": "جزئي",
    "Customer": "العميل",
    "Discount": "الخصم",
    "Discount %": "نسبة الخصم %",
    "Default Discount %": "نسبة الخصم الافتراضية %",
    "Email": "البريد الإلكتروني",
    "Group": "المجموعة",
    "Price": "السعر",
    "Unit Price": "سعر الوحدة",
    "Unit Cost": "تكلفة الوحدة",
    "Cost Price": "سعر التكلفة",
    "Sale Price": "سعر البيع",
    "Subtotal": "المجموع الفرعي",
    "Payment": "الدفع",
    "Description": "الوصف",
    "Items": "الأصناف",
    "Item": "الصنف",
    "Item Type": "نوع الصنف",
    "Line Items": "بنود الفاتورة",
    "Line Total": "إجمالي البند",
    "Ref": "مرجع",
    "Ref #": "رقم المرجع",
    "Reference": "المرجع",
    "Supplier": "المورد",
    "Other": "أخرى",
    "Address": "العنوان",
    "Method": "الطريقة",
    "Employee": "الموظف",
    "Employees": "الموظفون",
    "Account": "الحساب",
    "Source": "المصدر",
    "Cost": "التكلفة",
    "Cost (EGP)": "التكلفة (ج.م)",
    "Total Cost": "إجمالي التكلفة",
    "Total Value": "إجمالي القيمة",
    "Count": "العدد",
    "Entries": "القيود",
    "Balance": "الرصيد",
    "Revenue": "الإيرادات",
    "Total Revenue": "إجمالي الإيرادات",
    "Month": "الشهر",
    "From": "من",
    "To": "إلى",
    "From Date": "من تاريخ",
    "To Date": "إلى تاريخ",
    "Start date": "تاريخ البداية",
    "End date": "تاريخ النهاية",
    "Start": "البداية",
    "End": "النهاية",
    "Today": "اليوم",
    "Year": "سنة",
    "Custom": "مخصص",
    "Custom Range": "نطاق مخصص",
    "Choose date range": "اختر نطاق التاريخ",
    "Date range": "نطاق التاريخ",
    "Selected range": "النطاق المحدد",
    "Period": "الفترة",
    "File": "الملف",
    "Row": "صف",
    "Rows": "الصفوف",
    "Batch ID": "رقم الدفعة",
    "Batch #": "دفعة رقم",
    "Code": "الكود",
    "Label": "التسمية",
    "Module": "الوحدة",
    "Action": "الإجراء",
    "User": "المستخدم",
    "Role": "الدور",
    "Permissions": "الصلاحيات",
    "Created": "تاريخ الإنشاء",
    "Contact": "جهة الاتصال",
    "City / Area": "المدينة / المنطقة",
    "Terms": "الشروط",
    "Vendor": "المورد",
    "Debit": "مدين",
    "Credit": "دائن",
    "EGP": "ج.م",
    "EGP total": "الإجمالي بالجنيه",
    "kg": "كجم",
    "Counterparty": "الطرف المقابل",
    "Position": "الوظيفة",
    "Department": "القسم",
    "required": "مطلوب",
    "optional": "اختياري",
    "(optional)": "(اختياري)",

    /* ── Buttons / actions ── */
    "Cancel": "إلغاء",
    "Delete": "حذف",
    "Edit": "تعديل",
    "Save": "حفظ",
    "Apply": "تطبيق",
    "Close": "إغلاق",
    "Clear": "مسح",
    "View": "عرض",
    "Print": "طباعة",
    "Search": "بحث",
    "Next": "التالي",
    "Prev": "السابق",
    "Back": "رجوع",
    "Remove": "إزالة",
    "Refresh": "تحديث",
    "Export Excel": "تصدير إكسل",
    "Excel": "إكسل",
    "Add Product": "إضافة منتج",
    "Add Category": "إضافة فئة",
    "Category Name": "اسم الفئة",
    "Add Account": "إضافة حساب",
    "Add User": "إضافة مستخدم",
    "Add Group": "إضافة مجموعة",
    "Add Output": "إضافة ناتج",
    "Add Input": "إضافة مدخل",
    "Add Raw Material": "إضافة خامة",
    "Add target": "إضافة هدف",
    "Save User": "حفظ المستخدم",
    "Save Recipe": "حفظ الوصفة",
    "Save Entry": "حفظ القيد",
    "Record Payment": "تسجيل دفعة",
    "Record Delivery": "تسجيل توريد",
    "Log Attendance": "تسجيل الحضور",
    "Run Payroll": "تشغيل الرواتب",
    "Log Spoilage": "تسجيل تالف",
    "Log Weather": "تسجيل الطقس",
    "Log Feeding": "تسجيل تغذية",
    "Log Death": "تسجيل نفوق",
    "Log Emission": "تسجيل انبعاث",
    "Log Emission | Carbon | AZed ERP": "تسجيل انبعاث | الكربون | AZed ERP",
    "Receive Animals": "استلام حيوانات",
    "Create a new group…": "إنشاء مجموعة جديدة…",
    "Reset Password": "إعادة تعيين كلمة المرور",
    "Reset Pwd": "إعادة تعيين",
    "Update Password": "تحديث كلمة المرور",
    "Manage Factors": "إدارة المعاملات",
    "Analyze": "تحليل",
    "Receive": "استلام",
    "Log": "السجل",
    "Dry run": "تجربة بدون حفظ",
    "Full Payment": "دفع كامل",
    "Consignment": "بضاعة أمانة",
    "Cash": "نقدي",
    "Cash Receipt": "إيصال نقدي",
    "Expense Receipt": "إيصال مصروف",
    "Client Refund": "مرتجع عميل",
    "Refund Total": "إجمالي المرتجع",

    /* ── States / messages ── */
    "Loading…": "جارٍ التحميل…",
    "Loading...": "جارٍ التحميل...",
    "Could not load.": "تعذر التحميل.",
    "Could not load batches.": "تعذر تحميل الدفعات.",
    "Error.": "خطأ.",
    "No data": "لا توجد بيانات",
    "No products found": "لا توجد منتجات",
    "No products match.": "لا توجد منتجات مطابقة.",
    "No employees found": "لا يوجد موظفون",
    "No users found": "لا يوجد مستخدمون",
    "No farm selected": "لم يتم اختيار مزرعة",
    "No transactions in this period": "لا توجد معاملات في هذه الفترة",
    "Updated just now": "تم التحديث الآن",
    "skipped": "تم تخطيه",
    "Active": "نشط",
    "Inactive": "غير نشط",
    "Unknown": "غير معروف",
    "Sold": "مباع",
    "Reverted": "تم التراجع",
    "Present": "حاضر",
    "Late": "متأخر",
    "Leave": "إجازة",
    "Absent": "غائب",
    "Day Off": "يوم راحة",
    "Died": "نافق",
    "Deceased": "نافق",
    "All": "الكل",
    "All Farms": "كل المزارع",
    "All Types": "كل الأنواع",
    "All Statuses": "كل الحالات",
    "All Categories": "كل الفئات",
    "All Employees": "كل الموظفين",
    "All Expenses": "كل المصروفات",
    "All Batches": "كل الدفعات",
    "None": "لا يوجد",

    /* ── POS / Sales / B2B ── */
    "Net Sales": "صافي المبيعات",
    "Gross Sales": "إجمالي المبيعات",
    "Gross": "الإجمالي",
    "Cash Collected": "النقدية المحصلة",
    "Collected": "المحصل",
    "Unpaid Invoices": "فواتير غير مدفوعة",
    "Sales over time": "المبيعات عبر الزمن",
    "Recent transactions": "أحدث المعاملات",
    "Profit summary": "ملخص الأرباح",
    "Top B2B clients": "أكبر عملاء الجملة",
    "Select a client to view their price list.": "اختر عميلاً لعرض قائمة أسعاره.",
    "Select a client": "اختر عميلاً",
    "Search clients...": "بحث في العملاء...",
    "Search by name or SKU…": "بحث بالاسم أو الكود…",
    "Search by name or SKU...": "بحث بالاسم أو الكود...",
    "Type product name or SKU…": "اكتب اسم المنتج أو الكود…",
    "Search name, email, role...": "بحث بالاسم أو البريد أو الدور...",
    "General payment (no specific month)": "دفعة عامة (بدون شهر محدد)",
    "Payment Rate": "معدل السداد",
    "Last Purchase": "آخر شراء",
    "Total Spent": "إجمالي الإنفاق",
    "Net Spent": "صافي الإنفاق",
    "Failed to load customer segments": "تعذر تحميل شرائح العملاء",
    "Deliveries": "التوريدات",
    "Delivery #": "توريد رقم",
    "Total Deliveries": "إجمالي التوريدات",
    "This Month": "هذا الشهر",
    "Orders": "الطلبات",
    "Invoiced": "تمت فوترته",
    "Receipts": "الإيصالات",

    /* ── Inventory / products ── */
    "Current Stock": "المخزون الحالي",
    "Stock Levels": "مستويات المخزون",
    "Stock Value": "قيمة المخزون",
    "Low Stock": "مخزون منخفض",
    "Out of Stock": "نفد المخزون",
    "Stock In": "وارد المخزون",
    "Stock Out": "صادر المخزون",
    "Raw Material": "خامة",
    "Finished Product": "منتج نهائي",
    "Fresh": "طازج",
    "Packing": "تعبئة",
    "Packaging": "تعبئة",
    "Ingredient": "مكوّن",
    "Service": "خدمة",
    "Storage": "المخزن",
    "Choose storage —": "اختر المخزن —",
    "Choose source —": "اختر المصدر —",
    "Choose destination —": "اختر الوجهة —",
    "Choose group —": "اختر المجموعة —",
    "No Category —": "بدون فئة —",
    "No farm —": "بدون مزرعة —",
    "none —": "لا يوجد —",
    "General expense —": "مصروف عام —",
    "Product Category": "فئة المنتج",
    "Purchase Cost": "تكلفة الشراء",
    "Must match existing product": "يجب أن يطابق منتجاً موجوداً",
    "Existing product SKU": "كود منتج موجود",
    "Product name / display hint": "اسم المنتج / تلميح العرض",
    "Product Type": "نوع المنتج",
    "Select product": "اختر المنتج",

    /* ── Production / drying ── */
    "New Processing Batch": "دفعة تصنيع جديدة",
    "New Packaging Run": "تشغيلة تعبئة جديدة",
    "Materials Used": "الخامات المستخدمة",
    "Packs Created": "العبوات المنتجة",
    "Inputs": "المدخلات",
    "Outputs": "المخرجات",
    "Loss %": "نسبة الفاقد %",
    "Recipe": "الوصفة",
    "Production Report": "تقرير الإنتاج",
    "Spoilage Report": "تقرير التالف",
    "Spoilage": "التالف",
    "Spoilage breakdown": "تفصيل التالف",
    "Cause": "السبب",
    "Mold": "عفن",
    "Pest": "آفات",
    "Not from a specific farm": "ليس من مزرعة محددة",

    /* ── Farm ── */
    "Farm": "المزرعة",
    "Farm (optional)": "المزرعة (اختياري)",
    "Quality Notes": "ملاحظات الجودة",
    "Rainfall (mm)": "الأمطار (مم)",
    "Humidity (%)": "الرطوبة (%)",
    "Select farm...": "اختر المزرعة...",
    "Cost Breakdown by Category": "تفصيل التكاليف حسب الفئة",
    "Cost / Unit": "التكلفة / الوحدة",
    "Loading archived farms…": "جارٍ تحميل المزارع المؤرشفة…",
    "archive": "أرشفة",
    "Receive Date": "تاريخ الاستلام",
    "Received By": "استلمه",
    "Utilities by farm": "المرافق حسب المزرعة",
    "Top farms": "أعلى المزارع",
    "By quantity": "حسب الكمية",
    "Top products by intake": "أعلى المنتجات استلاماً",
    "Farm expenses": "مصروفات المزرعة",
    "Net contribution per farm": "صافي مساهمة كل مزرعة",
    "Operational signals": "مؤشرات تشغيلية",
    "Utility": "مرفق",
    "Consumption": "الاستهلاك",
    "Transport": "النقل",
    "Energy": "الطاقة",
    "Waste / Spoilage": "مخلفات / تالف",

    /* ── Animals ── */
    "Animal Group": "مجموعة الحيوانات",
    "Animal Groups": "مجموعات الحيوانات",
    "Headcount": "عدد الرؤوس",
    "Total Headcount": "إجمالي عدد الرؤوس",
    "Active Groups": "المجموعات النشطة",
    "Mortality Log": "سجل النفوق",
    "Feeding Log": "سجل التغذية",
    "Feedings Today": "تغذيات اليوم",
    "Deaths This Month": "النفوق هذا الشهر",
    "Cattle": "أبقار",
    "Poultry": "دواجن",
    "Sheep": "أغنام",
    "Goats": "ماعز",
    "Manage animal groups and track feed consumption.": "إدارة مجموعات الحيوانات وتتبع استهلاك الأعلاف.",
    "A group is a herd, flock, or pen of animals.": "المجموعة هي قطيع أو سرب أو حظيرة من الحيوانات.",
    "Purchase Cost — Total (EGP, optional)": "تكلفة الشراء — الإجمالي (ج.م، اختياري)",
    "Cost per Head (EGP, optional)": "التكلفة لكل رأس (ج.م، اختياري)",
    "Cost per head": "التكلفة لكل رأس",
    "Product (feed)": "المنتج (علف)",
    "Source Storage": "مخزن المصدر",
    "Records a death and reduces the group's headcount.": "يسجل حالة نفوق ويخفض عدد رؤوس المجموعة.",
    "Count (how many died)": "العدد (كم نفق)",
    "Count (how many received)": "العدد (كم تم استلامه)",
    "Illness / disease": "مرض",
    "Injury": "إصابة",
    "Old age": "شيخوخة",
    "Predator": "مفترس",
    "Weather / heat / cold": "طقس / حر / برد",
    "Birth complications": "مضاعفات ولادة",
    "Other (describe in note)": "أخرى (اذكرها في الملاحظة)",
    "Purchase (bought in)": "شراء (من الخارج)",
    "Birth (born on farm)": "ولادة (في المزرعة)",
    "Transfer in": "تحويل وارد",
    "New Group Name": "اسم المجموعة الجديدة",
    "Supplier / Source": "المورد / المصدر",
    "Combined Animal Cost Analysis": "تحليل تكاليف الحيوانات المجمع",

    /* ── HR / payroll ── */
    "Attendance": "الحضور",
    "Payroll": "الرواتب",
    "Base Salary": "الراتب الأساسي",
    "Net Salary": "صافي الراتب",
    "Total to Pay": "إجمالي المستحق",
    "Manual Ded.": "خصم يدوي",
    "Day Ded.": "خصم أيام",
    "Total Ded.": "إجمالي الخصومات",
    "Days": "الأيام",
    "Bonuses": "المكافآت",
    "Hire Date": "تاريخ التعيين",
    "Loans & Deductions": "السلف والخصومات",
    "Salary & Wages": "الرواتب والأجور",
    "Clear HR Data": "مسح بيانات الموارد البشرية",

    /* ── Accounting / reports ── */
    "Total Expenses": "إجمالي المصروفات",
    "Expense": "مصروف",
    "Carbon overview": "نظرة عامة على الكربون",
    "Carbon category totals": "إجماليات فئات الكربون",
    "Category Breakdown": "تفصيل الفئات",
    "Operating Insights": "رؤى تشغيلية",
    "Highest source": "أعلى مصدر",
    "Highest source kg": "أعلى مصدر (كجم)",
    "Target status": "حالة الهدف",
    "Progress uses selected range total": "يُحتسب التقدم من إجمالي النطاق المحدد",
    "Reduction Targets": "أهداف الخفض",
    "Reduction targets": "أهداف الخفض",
    "Target kg CO₂e": "الهدف كجم مكافئ CO₂",
    "Emission Logs": "سجلات الانبعاثات",
    "Emission logs": "سجلات الانبعاثات",
    "Emission Source": "مصدر الانبعاث",
    "Emission Factors": "معاملات الانبعاث",
    "No active emission factors": "لا توجد معاملات انبعاث نشطة",
    "No emission factors found.": "لم يتم العثور على معاملات انبعاث.",
    "CO₂e coefficients used to calculate emissions": "معاملات مكافئ CO₂ المستخدمة في حساب الانبعاثات",
    "Record a CO₂-equivalent emission event": "تسجيل حدث انبعاث بمكافئ CO₂",
    "kg CO₂e": "كجم مكافئ CO₂",
    "CO₂e": "مكافئ CO₂",
    "Key": "المفتاح",
    "Factor": "المعامل",
    "Delete log entry": "حذف القيد",
    "Delete target": "حذف الهدف",
    "No target for this period yet.": "لا يوجد هدف لهذه الفترة بعد.",
    "No emissions logged for this period.": "لا توجد انبعاثات مسجلة لهذه الفترة.",

    /* ── Import ── */
    "Expected Excel Columns": "أعمدة الإكسل المتوقعة",
    "Import Mode": "وضع الاستيراد",
    "Historical Sales": "مبيعات تاريخية",
    "Historical B2B Sales": "مبيعات جملة تاريخية",
    "Sale date (any historical date)": "تاريخ البيع (أي تاريخ سابق)",
    "Also adjust stock — use with extreme caution": "تعديل المخزون أيضاً — استخدم بحذر شديد",
    "Force import even if duplicates detected": "فرض الاستيراد حتى مع وجود تكرارات",
    "Dry run - nothing was saved": "تجربة — لم يتم حفظ أي شيء",
    "DRY RUN — nothing was saved": "تجربة — لم يتم حفظ أي شيء",
    "- preview without saving (recommended first step)": "- معاينة بدون حفظ (الخطوة الأولى الموصى بها)",
    "— recommended": "— موصى به",

    /* ── Users / passwords ── */
    "My Password": "كلمة مروري",
    "Change Your Password": "تغيير كلمة المرور",
    "You must enter your current password to set a new one.": "يجب إدخال كلمة المرور الحالية لتعيين كلمة جديدة.",
    "Current Password": "كلمة المرور الحالية",
    "New Password": "كلمة المرور الجديدة",
    "Confirm New Password": "تأكيد كلمة المرور الجديدة",
    "Confirm Password": "تأكيد كلمة المرور",
    "Password": "كلمة المرور",
    "Enter your current password": "أدخل كلمة المرور الحالية",
    "Repeat new password": "أعد كتابة كلمة المرور الجديدة",
    "Repeat your new password": "أعد كتابة كلمة المرور الجديدة",
    "Create a new system user": "إنشاء مستخدم جديد في النظام",
    "Reset password for user": "إعادة تعيين كلمة مرور المستخدم",
    "Cashier": "كاشير",
    "Manager": "مدير",
    "Accountant": "محاسب",
    "HR": "موارد بشرية",
    "Viewer": "مشاهد",
    "Pages Access": "صلاحيات الصفحات",
    "Page access": "صلاحية الصفحات",
    "(select pages first — then tabs & actions appear)": "(اختر الصفحات أولاً — ثم تظهر التبويبات والإجراءات)",
    "Account is active (user can log in)": "الحساب نشط (يمكن للمستخدم تسجيل الدخول)",
    "No extra tabs/actions for the selected pages.": "لا توجد تبويبات/إجراءات إضافية للصفحات المحددة.",
    "All pages, tabs, and actions": "كل الصفحات والتبويبات والإجراءات",
    "Full unrestricted access": "صلاحية كاملة بدون قيود",
    "e.g. Ahmed Hassan": "مثال: أحمد حسن",
    "name@example.com": "name@example.com",
    "e.g. 50": "مثال: 50",
    "e.g. May carbon budget": "مثال: ميزانية كربون مايو",
    "e.g. Diesel for irrigation pump, Field 3": "مثال: ديزل لمضخة الري، حقل 3",

    /* ── Production page (batches / packaging / recipes / drying / spoilage) ── */
    "Production — AZed ERP": "الإنتاج — AZed ERP",
    "Production & Processing": "الإنتاج والتصنيع",
    "Process raw materials, package products, track spoilage": "معالجة الخامات وتعبئة المنتجات وتتبع التالف",
    "Recipes": "الوصفات",
    "Processing Recipe": "وصفة تصنيع",
    "Packaging Recipe": "وصفة تعبئة",
    "New Packaging Recipe": "وصفة تعبئة جديدة",
    "Processing": "تصنيع",
    "Drying": "التجفيف",
    "New Drying Batch": "دفعة تجفيف جديدة",
    "Stages": "المراحل",
    "Stage": "مرحلة",
    "Add Next Stage": "إضافة المرحلة التالية",
    "Inputs per 1 pack": "المدخلات لكل عبوة",
    "Outputs per 1 pack": "المخرجات لكل عبوة",
    "Output per 1 pack": "الناتج لكل عبوة",
    "Inputs per batch": "المدخلات لكل دفعة",
    "Outputs per batch": "المخرجات لكل دفعة",
    "Use for Packaging": "استخدام للتعبئة",
    "Use in Batch": "استخدام في دفعة",
    "No recipes saved yet.": "لا توجد وصفات محفوظة بعد.",
    "No batches yet.": "لا توجد دفعات بعد.",
    "No drying batches yet.": "لا توجد دفعات تجفيف بعد.",
    "No packaging runs yet.": "لا توجد تشغيلات تعبئة بعد.",
    "Save a reusable formula": "حفظ تركيبة قابلة لإعادة الاستخدام",
    "Save a standard processing formula": "حفظ تركيبة تصنيع قياسية",
    "Define inputs and outputs PER 1 PACK": "حدد المدخلات والمخرجات لكل عبوة واحدة",
    "Recipe Name": "اسم الوصفة",
    "Recipe name is required": "اسم الوصفة مطلوب",
    "Recipe saved!": "تم حفظ الوصفة!",
    "Recipe deleted": "تم حذف الوصفة",
    "Load from Recipe (optional)": "التحميل من وصفة (اختياري)",
    "Start blank or select a recipe": "ابدأ فارغاً أو اختر وصفة",
    "Select packaging recipe": "اختر وصفة التعبئة",
    "Select a packaging recipe": "اختر وصفة تعبئة",
    "Select a recipe and enter how many packs to produce": "اختر وصفة وأدخل عدد العبوات المطلوب إنتاجها",
    "Number of Packs to Make": "عدد العبوات المطلوب إنتاجها",
    "Packs to create": "العبوات المطلوب إنشاؤها",
    "Run Packaging": "تشغيل التعبئة",
    "Run Batch": "تشغيل الدفعة",
    "Start Batch": "بدء الدفعة",
    "Start Drying Batch": "بدء دفعة التجفيف",
    "Started": "بدأت",
    "completed": "مكتمل",
    "Finalize": "إنهاء",
    "Finalize Batch": "إنهاء الدفعة",
    "Finalize Drying Batch": "إنهاء دفعة التجفيف",
    "Save & Open Next Stage": "حفظ وفتح المرحلة التالية",
    "Save Changes": "حفظ التغييرات",
    "Auto-Calculated Loss": "الفاقد المحسوب تلقائياً",
    "Loss is calculated automatically from input vs output quantities": "يُحسب الفاقد تلقائياً من مقارنة كميات المدخلات بالمخرجات",
    "Raw Materials Going In": "الخامات الداخلة",
    "Raw Materials Used (Inputs)": "الخامات المستخدمة (مدخلات)",
    "Finished Products Created (Outputs)": "المنتجات النهائية المنتجة (مخرجات)",
    "Add Finished Product": "إضافة منتج نهائي",
    "Add at least one input": "أضف مدخلاً واحداً على الأقل",
    "Add at least one output": "أضف ناتجاً واحداً على الأقل",
    "Add at least one raw material": "أضف خامة واحدة على الأقل",
    "Add at least one finished product": "أضف منتجاً نهائياً واحداً على الأقل",
    "Enter a quantity greater than 0": "أدخل كمية أكبر من صفر",
    "Enter number of packs to make": "أدخل عدد العبوات المطلوب إنتاجها",
    "Could not load batch": "تعذر تحميل الدفعة",
    "Failed to load.": "تعذر التحميل.",
    "Inputs Used": "المدخلات المستخدمة",
    "Outputs Created": "المخرجات المنتجة",
    "Inputs for the NEW stage": "مدخلات المرحلة الجديدة",
    "Outputs of the CURRENT stage": "مخرجات المرحلة الحالية",
    "What did the current stage produce? Stock will be credited.": "ماذا أنتجت المرحلة الحالية؟ سيُضاف الناتج إلى المخزون.",
    "What will happen": "ما الذي سيحدث",
    "Materials needed": "الخامات المطلوبة",
    "Final outputs": "المخرجات النهائية",
    "Final yield %": "نسبة الناتج النهائي %",
    "Latest output": "آخر ناتج",
    "Latest stage": "آخر مرحلة",
    "Original input": "المدخل الأصلي",
    "Batch Notes": "ملاحظات الدفعة",
    "Completion notes": "ملاحظات الإنهاء",
    "Notes for the new stage (optional)": "ملاحظات للمرحلة الجديدة (اختياري)",
    "Notes on the current stage (optional)": "ملاحظات على المرحلة الحالية (اختياري)",
    "Label for the new stage (optional)": "تسمية المرحلة الجديدة (اختياري)",
    "Detail (optional)": "تفاصيل (اختياري)",
    "Any additional details...": "أي تفاصيل إضافية...",
    "Farm Source": "مزرعة المصدر",
    "Farm Source (optional)": "مزرعة المصدر (اختياري)",
    "Qty Lost": "الكمية المفقودة",
    "Quantity Lost": "الكمية المفقودة",
    "Log Drying Spoilage": "تسجيل تالف التجفيف",
    "No spoilage recorded yet. Click \"Log Spoilage\" to start.": "لم يُسجل أي تالف بعد. اضغط \"تسجيل تالف\" للبدء.",
    "Select reason...": "اختر السبب...",
    "Select a product": "اختر منتجاً",
    "Select a date": "اختر تاريخاً",
    "Damaged": "تالف",
    "Expired": "منتهي الصلاحية",
    "Overripe": "ناضج زيادة",
    "Heat damage": "تلف حراري",
    "Water damage": "تلف مائي",
    "Weather": "الطقس",
    "e.g. Moringa Powder Processing": "مثال: تصنيع مسحوق المورينجا",
    "e.g. Grinding": "مثال: طحن",
    "e.g. Morning harvest": "مثال: حصاد الصباح",
    "e.g. 1kg fresh leaves to 100g powder": "مثال: 1 كجم أوراق طازجة إلى 100 جم مسحوق",
    "e.g. Started sun-drying tomatoes": "مثال: بدء تجفيف الطماطم شمسياً",
    "e.g. Sun-drying completed after 8 days": "مثال: اكتمل التجفيف الشمسي بعد 8 أيام",
    "e.g. Production complete; 2.1kg powder yielded": "مثال: اكتمل الإنتاج؛ الناتج 2.1 كجم مسحوق",
    "e.g. Rain damaged third tray": "مثال: المطر أتلف الصينية الثالثة",

    /* ── Misc ── */
    "Users — AZed ERP": "المستخدمون — AZed ERP",
    "Change Password — AZed ERP": "تغيير كلمة المرور — AZed ERP",
    "Carbon Footprint | AZed ERP": "البصمة الكربونية | AZed ERP",
    "Emission Factors | Carbon | AZed ERP": "معاملات الانبعاث | الكربون | AZed ERP",
    "Animals — AZed ERP": "الحيوانات — AZed ERP"
  };

  /* Regex rules for dynamic strings (applied when exact lookup fails). */
  var REGEX_RULES = [
    [/^Good morning,(\s*)(.*)$/, "صباح الخير، $2"],
    [/^Good afternoon,(\s*)(.*)$/, "مساء الخير، $2"],
    [/^Good evening,(\s*)(.*)$/, "مساء الخير، $2"],
    [/^Page (\d+) of (\d+)$/, "صفحة $1 من $2"],
    [/^Showing (\d+) of (\d+)$/, "عرض $1 من $2"],
    [/^Error:\s*([\s\S]*)$/, "خطأ: $1"]
  ];

  /* ──────────────────────────────────────────────────────────────────
   * Lookup
   * ────────────────────────────────────────────────────────────────── */
  var missing = {};

  function lookup(raw) {
    var text = raw.replace(/\s+/g, " ").trim();
    if (!text || !/[A-Za-z]/.test(text)) return null;
    // Already (partially) Arabic — never re-translate or report as missing.
    if (/[\u0600-\u06FF]/.test(text)) return null;

    if (DICT.hasOwnProperty(text)) return DICT[text];

    // Strip a leading emoji / symbol cluster and trailing *, :, …, .
    var m = text.match(/^([^A-Za-z(]*)([\s\S]*?)([\s]*[\*:…]+|[\s]*\.\.\.)?$/);
    if (m) {
      var prefix = m[1] || "";
      var core = (m[2] || "").trim();
      var suffix = m[3] || "";
      if (core && core !== text && DICT.hasOwnProperty(core)) {
        return prefix + DICT[core] + suffix;
      }
    }

    // Trailing "*" / ":" only
    var stripped = text.replace(/[\s]*[\*:…]+$/, "").trim();
    if (stripped !== text && DICT.hasOwnProperty(stripped)) {
      return DICT[stripped] + text.slice(stripped.length);
    }

    for (var i = 0; i < REGEX_RULES.length; i += 1) {
      if (REGEX_RULES[i][0].test(text)) {
        return text.replace(REGEX_RULES[i][0], REGEX_RULES[i][1]);
      }
    }

    // Collect for the developer (only meaningful strings)
    if (text.length >= 2 && text.length <= 80) missing[text] = true;
    return null;
  }

  /* ──────────────────────────────────────────────────────────────────
   * DOM translation engine
   * ────────────────────────────────────────────────────────────────── */
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, CODE: 1, PRE: 1 };
  var ATTRS = ["placeholder", "title", "aria-label"];
  var applying = false;

  function eachTextNode(root, fn) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var p = node.parentNode;
        if (!p || SKIP_TAGS[p.nodeName]) return NodeFilter.FILTER_REJECT;
        if (p.closest && p.closest("[data-no-i18n],[contenteditable='true']")) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n;
    while ((n = walker.nextNode())) fn(n);
  }

  function eachElement(root, fn) {
    if (root.nodeType === 1) fn(root);
    if (root.querySelectorAll) {
      var all = root.querySelectorAll("[placeholder],[title],[aria-label],input[type=button],input[type=submit]");
      for (var i = 0; i < all.length; i += 1) fn(all[i]);
    }
  }

  function translateTextNode(node) {
    var p = node.parentNode;
    if (!p || SKIP_TAGS[p.nodeName]) return;
    if (p.closest && p.closest("[data-no-i18n],[contenteditable='true']")) return;
    var val = node.nodeValue;
    if (!val || !val.trim()) return;
    var translated = lookup(val);
    if (translated == null) return;
    if (node.__i18nOriginal === undefined) node.__i18nOriginal = val;
    // Preserve surrounding whitespace
    var lead = val.match(/^\s*/)[0];
    var tail = val.match(/\s*$/)[0];
    var next = lead + translated + tail;
    if (node.nodeValue !== next) node.nodeValue = next;
  }

  function translateElementAttrs(el) {
    if (el.closest && el.closest("[data-no-i18n]")) return;
    for (var i = 0; i < ATTRS.length; i += 1) {
      var a = ATTRS[i];
      var v = el.getAttribute && el.getAttribute(a);
      if (!v) continue;
      var t = lookup(v);
      if (t == null) continue;
      if (!el.__i18nAttrOrig) el.__i18nAttrOrig = {};
      if (el.__i18nAttrOrig[a] === undefined) el.__i18nAttrOrig[a] = v;
      if (v !== t) el.setAttribute(a, t);
    }
    if (el.tagName === "INPUT" && (el.type === "button" || el.type === "submit") && el.value) {
      var tv = lookup(el.value);
      if (tv != null) {
        if (!el.__i18nAttrOrig) el.__i18nAttrOrig = {};
        if (el.__i18nAttrOrig.value === undefined) el.__i18nAttrOrig.value = el.value;
        if (el.value !== tv) el.value = tv;
      }
    }
  }

  function translateTree(root) {
    applying = true;
    try {
      eachTextNode(root, translateTextNode);
      eachElement(root, translateElementAttrs);
    } finally {
      applying = false;
    }
  }

  function restoreTree(root) {
    applying = true;
    try {
      eachTextNode(root, function (node) {
        if (node.__i18nOriginal !== undefined) {
          node.nodeValue = node.__i18nOriginal;
          node.__i18nOriginal = undefined;
        }
      });
      var all = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (var i = 0; i < all.length; i += 1) {
        var el = all[i];
        if (!el.__i18nAttrOrig) continue;
        for (var a in el.__i18nAttrOrig) {
          if (a === "value") el.value = el.__i18nAttrOrig[a];
          else el.setAttribute(a, el.__i18nAttrOrig[a]);
        }
        el.__i18nAttrOrig = undefined;
      }
    } finally {
      applying = false;
    }
  }

  function translateTitle() {
    var t = lookup(document.title || "");
    if (t != null) {
      if (window.__i18nTitleOrig === undefined) window.__i18nTitleOrig = document.title;
      document.title = t;
    }
  }

  function restoreTitle() {
    if (window.__i18nTitleOrig !== undefined) {
      document.title = window.__i18nTitleOrig;
      window.__i18nTitleOrig = undefined;
    }
  }

  /* ──────────────────────────────────────────────────────────────────
   * RTL + Arabic font + minor CSS fixes
   * ────────────────────────────────────────────────────────────────── */
  function installArStyles() {
    if (document.getElementById("app-i18n-ar-style")) return;
    var font = document.createElement("link");
    font.id = "app-i18n-ar-font";
    font.rel = "stylesheet";
    font.href = "https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800&display=swap";
    (document.head || document.documentElement).appendChild(font);

    var style = document.createElement("style");
    style.id = "app-i18n-ar-style";
    style.textContent = [
      // Arabic UI font everywhere except code/monospace contexts.
      "html[lang='ar'] body,",
      "html[lang='ar'] button,",
      "html[lang='ar'] input,",
      "html[lang='ar'] select,",
      "html[lang='ar'] textarea,",
      "html[lang='ar'] :where(h1,h2,h3,h4,h5,h6,p,span,a,td,th,label,div):not(code):not(pre):not([class*='mono'])",
      "{ font-family:'Cairo','DM Sans',sans-serif; letter-spacing:0; }",
      // Keep numeric / date / phone inputs LTR inside RTL layout.
      "html[dir='rtl'] input[type='number'],",
      "html[dir='rtl'] input[type='date'],",
      "html[dir='rtl'] input[type='time'],",
      "html[dir='rtl'] input[type='tel'],",
      "html[dir='rtl'] input[type='email']",
      "{ direction:ltr; text-align:right; }",
      // Numbers and currency read better LTR.
      "html[dir='rtl'] :where(.mono,[class*='mono'],[class*='amount'],[class*='num']) { direction:ltr; unicode-bidi:isolate; }",
      // Production page polish: flip the recipe-card accent gradient and keep
      // \"qty unit\" values (e.g. \"5 kg\") rendered LTR inside RTL cards.
      "html[dir='rtl'] .recipe-card.processing::before { background:linear-gradient(270deg,var(--orange),transparent); }",
      "html[dir='rtl'] .recipe-card.packaging::before { background:linear-gradient(270deg,var(--teal),transparent); }",
      "html[dir='rtl'] .recipe-item span:last-child { direction:ltr; unicode-bidi:isolate; }",
      // Language toggle button (mirrors the theme toggle styling).
      ".app-lang-toggle{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;min-width:36px;border-radius:10px;border:1px solid var(--app-control-border,#334155);background:var(--app-control-bg,#1E293B);color:var(--app-control-text,#cbd5e1);cursor:pointer;font-size:13px;font-weight:700;line-height:1;transition:border-color .18s ease,color .18s ease,background .18s ease,transform .18s ease;}",
      ".app-lang-toggle:hover{border-color:var(--app-control-border-hover,#475569);background:var(--app-control-bg-hover,#334155);color:var(--app-control-text-strong,#F8FAFC);transform:scale(1.06);}",
      ".app-lang-toggle--floating{position:fixed;bottom:18px;left:18px;z-index:9999;box-shadow:0 8px 24px rgba(0,0,0,.35);}",
      "html[dir='rtl'] .app-lang-toggle--floating{left:auto;right:18px;}"
    ].join("\n");
    (document.head || document.documentElement).appendChild(style);
  }

  /* ──────────────────────────────────────────────────────────────────
   * Language state
   * ────────────────────────────────────────────────────────────────── */
  function readLang() {
    try { return localStorage.getItem(LANG_KEY) === "ar" ? "ar" : "en"; } catch (_) { return "en"; }
  }

  function persistLang(lang) {
    try { localStorage.setItem(LANG_KEY, lang === "ar" ? "ar" : "en"); } catch (_) {}
  }

  var observer = null;

  function startObserver() {
    if (observer || !document.body) return;
    observer = new MutationObserver(function (mutations) {
      if (applying || readLang() !== "ar") return;
      for (var i = 0; i < mutations.length; i += 1) {
        var m = mutations[i];
        if (m.type === "childList") {
          for (var j = 0; j < m.addedNodes.length; j += 1) {
            var node = m.addedNodes[j];
            if (node.nodeType === 3) translateTextNode(node);
            else if (node.nodeType === 1 && !SKIP_TAGS[node.nodeName]) translateTree(node);
          }
        } else if (m.type === "characterData" && m.target) {
          translateTextNode(m.target);
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  function stopObserver() {
    if (observer) { observer.disconnect(); observer = null; }
  }

  function syncToggleButtons(lang) {
    var label = lang === "ar" ? "Switch to English" : "التبديل إلى العربية";
    document.querySelectorAll("[data-lang-toggle],.app-lang-toggle").forEach(function (btn) {
      btn.textContent = lang === "ar" ? "EN" : "ع";
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
      btn.setAttribute("data-no-i18n", "");
    });
  }

  function applyLang(lang, opts) {
    var settings = opts || {};
    lang = lang === "ar" ? "ar" : "en";
    if (lang === "ar") {
      installArStyles();
      document.documentElement.setAttribute("dir", "rtl");
      document.documentElement.setAttribute("lang", "ar");
      if (document.body) {
        translateTree(document.body);
        translateTitle();
        startObserver();
      }
    } else {
      document.documentElement.setAttribute("dir", "ltr");
      document.documentElement.setAttribute("lang", "en");
      stopObserver();
      if (document.body) {
        restoreTree(document.body);
        restoreTitle();
      }
    }
    syncToggleButtons(lang);
    if (settings.persist !== false) persistLang(lang);
    try {
      window.dispatchEvent(new CustomEvent("app:langchange", { detail: { lang: lang } }));
    } catch (_) {}
    return lang;
  }

  /* ──────────────────────────────────────────────────────────────────
   * Toggle button injection
   * ────────────────────────────────────────────────────────────────── */
  function mountToggle() {
    if (!document.body) return;
    if (document.querySelector("[data-lang-toggle],.app-lang-toggle")) {
      syncToggleButtons(readLang());
      return;
    }
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "app-lang-toggle";
    btn.setAttribute("data-lang-toggle", "");
    btn.setAttribute("data-no-i18n", "");

    // Prefer sitting right next to the theme toggle; otherwise float.
    var themeBtn = document.querySelector("[data-theme-toggle],#mode-btn,#themeToggle,.app-theme-toggle");
    if (themeBtn && themeBtn.parentNode) {
      themeBtn.parentNode.insertBefore(btn, themeBtn);
    } else {
      btn.classList.add("app-lang-toggle--floating");
      document.body.appendChild(btn);
    }
    syncToggleButtons(readLang());
  }

  if (!window.__appLangToggleBound) {
    window.__appLangToggleBound = true;
    document.addEventListener("click", function (event) {
      var trigger = event.target && event.target.closest && event.target.closest("[data-lang-toggle],.app-lang-toggle");
      if (!trigger || !document.contains(trigger)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      applyLang(readLang() === "ar" ? "en" : "ar");
    }, true);
  }

  window.addEventListener("storage", function (event) {
    if (event.key !== LANG_KEY) return;
    applyLang(readLang(), { persist: false });
  });

  /* ──────────────────────────────────────────────────────────────────
   * Public API + boot
   * ────────────────────────────────────────────────────────────────── */
  window.__i18n = {
    get: readLang,
    set: function (lang) { return applyLang(lang); },
    toggle: function () { return applyLang(readLang() === "ar" ? "en" : "ar"); },
    translate: function (root) { if (readLang() === "ar") translateTree(root || document.body); },
    /** Untranslated strings seen this session — paste new DICT entries from this. */
    missing: function () { return Object.keys(missing).sort(); },
    dict: DICT,
    key: LANG_KEY
  };

  function boot() {
    installArStyles();
    mountToggle();
    applyLang(readLang(), { persist: false });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();