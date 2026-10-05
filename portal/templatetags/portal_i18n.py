from django import template

from portal.i18n import display_choice, display_validation_message

register = template.Library()
register.filter("portal_choice", display_choice)
register.filter("portal_validation", display_validation_message)
