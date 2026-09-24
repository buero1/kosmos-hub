"""The writing user owns signature identity, not the sender mailbox or worker."""
from datetime import UTC, datetime, timedelta

import pytest

from app.core.mailbox_actor import mailbox_actor
from app.core.templates import create_templates
from app.services.email_composer_settings import EmailComposerSettingsService
from app.services.hub_mailbox_transport import HubMailboxTransportDelivery, HubMailboxTransportService
from app.services.scheduled_emails import ScheduledEmailService
from app.services.template_placeholders import USER_PLACEHOLDERS, email_placeholders, pdf_placeholders
from test_hub_email_composition import template, contact, draft_payload
from test_hub_mailbox_operations import env, inbound, key, mailbox


SIGNATURE = '<p>Viele Gruesse<br>${User.Name}</p><p>${User.FirstName} ${User.LastName}</p>'


def configure(env):
    env.sales.first_name, env.sales.last_name = 'Steffi', 'Muster'
    env.admin.first_name, env.admin.last_name = 'Admin', 'Andere'
    settings = EmailComposerSettingsService(db=env.db)
    settings.configure_signature(signature_html=SIGNATURE)
    env.db.commit()
    return settings


def test_render_signature_is_personalized_readonly_and_actor_scoped(env):
    settings = configure(env)
    assert 'Steffi Muster' in settings.render_signature(actor='sales')
    assert 'Admin Andere' in settings.render_signature(actor='admin')
    token = mailbox_actor.set('sales')
    try:
        assert 'Steffi Muster' in settings.render_signature(actor='admin')
    finally:
        mailbox_actor.reset(token)
    assert settings.get_runtime_settings().signature_html == SIGNATURE
    assert not env.db.dirty and not env.db.new


@pytest.mark.parametrize('actor', [None, 'unknown', 'inactive'])
def test_missing_actor_never_impersonates_a_mailbox_owner(env, actor):
    settings = configure(env)
    env.sales.is_active = False
    env.db.flush()
    rendered = settings.render_signature(actor='sales' if actor == 'inactive' else actor)
    assert 'Ihr Kosmos Team' in rendered
    assert '${' not in rendered and 'Admin Andere' not in rendered and 'Steffi' not in rendered


def test_user_names_are_html_escaped_single_pass_with_blank_and_legacy_support(env):
    settings = configure(env)
    env.sales.first_name, env.sales.last_name = '<b>A & B</b>', None
    env.db.flush()
    settings.configure_signature(signature_html='<p>${User.Name}|{{ User.FirstName }}|${User.LastName}</p>')
    assert settings.render_signature(actor='sales') == '<p>&lt;b&gt;A &amp; B&lt;/b&gt;|&lt;b&gt;A &amp; B&lt;/b&gt;|</p>'
    env.sales.first_name = '${User.LastName}'
    env.sales.last_name = 'Literal'
    env.db.flush()
    assert '${User.LastName} Literal' in settings.render_signature(actor='sales')


@pytest.mark.parametrize('mode', ['customer', 'general', 'invoice'])
@pytest.mark.parametrize('signature_token', ['${Company.EmailSignature}', '${userSignature}'])
def test_templates_resolve_nested_signature_and_cannot_override_writing_user(env, mode, signature_token):
    configure(env)
    contact(env, env.own)
    template(env, subject='Von ${User.Name}', content='<p>${User.FirstName} ${User.LastName}</p>' + signature_token)
    communications = mailbox(env, 'sales').communications
    override = {'User.Name': 'Wrong Mailbox Owner', 'User.FirstName': 'Wrong'}
    if mode == 'customer':
        rendered = communications.get_email_template(template_id='source', customer_id=env.own.id, template_values=override)
    elif mode == 'general':
        rendered = communications.get_email_template_preview(template_id='source', template_values=override)
    else:
        recipient = next(item for item in communications.list_recipients(customer_id=env.own.id) if item.email == 'alice@example.test')
        rendered = communications.render_invoice_email_template(template_id='source', customer_id=env.own.id,
            recipient=recipient, invoice_values=override)
    assert rendered.subject == 'Von Steffi Muster'
    assert 'Steffi Muster' in rendered.content and 'Wrong' not in rendered.content
    assert '${' not in rendered.content and rendered.unresolved_placeholders == ()
    assert signature_token in communications.get_email_template_source(template_id='source').content


@pytest.mark.parametrize('linked', [True, False])
@pytest.mark.parametrize('action', ['reply', 'reply_all'])
def test_reply_signature_uses_author_but_does_not_rewrite_quoted_message(env, linked, action):
    configure(env)
    contact(env, env.own)
    message = inbound(env, customer=env.own if linked else None, message_id='reply-source',
        **{'from': [{'email': 'alice@example.test', 'name': 'Alice'}], 'sender': 'alice@example.test',
           'content': '<p>Quoted literal ${User.Name}</p>'})
    rendered = env.limited.query('emails.compose.preview', {'email_key': key(message), 'action': action})
    assert 'Steffi Muster' in rendered['content']
    assert 'Quoted literal ${User.Name}' in rendered['content']


def test_template_draft_keeps_author_when_another_user_opens_it(env):
    settings = configure(env)
    template(env, content='<p>Nachricht</p>${Company.EmailSignature}', hub_context_module='general')
    result = env.limited.execute('emails.drafts.from_template', {'template_id': 'source',
        'context_module': 'general', 'sender_email': 'sender@example.test', 'recipient_email': 'alice@example.test'})
    original = draft_payload(env, result)['content']
    settings.configure_signature(signature_html='<p>Changed for future messages</p>')
    reopened = mailbox(env, 'admin').get_draft_compose_context(draft_id=result.record_id)
    assert reopened['content'] == original and 'Steffi Muster' in original


def test_scheduled_delivery_keeps_composed_author_and_manual_edits(env, monkeypatch):
    settings = configure(env)
    now = datetime(2026, 9, 24, 10, 0)
    clock = {'now': now}
    monkeypatch.setattr(ScheduledEmailService, '_utc_now', staticmethod(lambda: clock['now']))
    deliveries = []
    def send(self, **values):
        deliveries.append(values)
        return HubMailboxTransportDelivery(message_id=values['message_id'], sent_at=clock['now'].replace(tzinfo=UTC))
    monkeypatch.setattr(HubMailboxTransportService, 'send', send)
    service = ScheduledEmailService(db=env.db, cipher=env.cipher, attachment_storage=env.storage)
    content = settings.render_signature(actor='sales') + '<p>Manually added</p>'
    scheduled = service.schedule(actor='sales', scheduled_at=now + timedelta(hours=1),
        sender_email='sender@example.test', recipient_email='alice@example.test', recipient_name='Alice',
        subject='Test', content=content, cc_emails='', attachments=())
    env.db.commit()
    settings.configure_signature(signature_html='<p>New global signature</p>')
    env.sales.last_name = 'Changed'
    env.db.commit()
    assert service.get_compose_context(scheduled_email_id=scheduled.id)['content'] == content
    clock['now'] += timedelta(hours=1)
    assert service.process_due().sent == 1
    assert len(deliveries) == 1
    assert 'Steffi Muster' in deliveries[0]['html_content'] and 'Manually added' in deliveries[0]['html_content']
    assert 'New global signature' not in deliveries[0]['html_content'] and 'Steffi Changed' not in deliveries[0]['html_content']


def test_user_tokens_are_searchable_in_email_and_signature_but_not_pdf():
    tokens = {item.token for item in USER_PLACEHOLDERS}
    assert tokens == {'${User.Name}', '${User.FirstName}', '${User.LastName}'}
    assert tokens <= {item.token for item in email_placeholders()}
    assert tokens.isdisjoint(item.token for item in pdf_placeholders('invoices'))
    html = create_templates(directory='app/templates').get_template('partials/template_placeholder_picker.html').render(
        placeholder_options=USER_PLACEHOLDERS, placeholder_picker_id='signature-placeholder-results')
    assert all('data-token="' + token + '"' in html for token in tokens)
