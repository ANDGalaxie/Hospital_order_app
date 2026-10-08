from django import template
from finance.services.finance_analysis_service import format_money

register = template.Library()
register.filter("expense_money", format_money)
