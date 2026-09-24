<?php
require __DIR__.'/support/bridge-wordpress-fixture.php';
use KosmosBridge\Options;
use KosmosBridge\Plugin;
use KosmosBridge\Registration\OptionStore;
use KosmosBridge\Registration\SecretStore;
use KosmosBridge\Registration\RegistrationState;
use KosmosBridge\Registration\Registrar;
use KosmosBridge\Registration\PayloadFactory;
use KosmosBridge\Http\RegistrationClient;

reset_fixture(array(Options::SITE_UUID=>'legacy',Options::SITE_SECRET=>'old-key',Options::LAST_SUCCESS_AT=>'yesterday'));
$responses=array($success);
check((new Registrar())->register(true), 'Unbound legacy success triggers full registration');
check(end($requests)['payload']['heartbeat']===false, 'Unknown identity cannot send heartbeat');
check(end($requests)['payload']['site_secret']==='old-key', 'Bootstrap sends matching key');
check(RegistrationState::is_registered(), 'Success bound to current identity');
check(Options::get_registration_status()==='ok' && Options::get_last_success_at()!=='', 'Current success visible');
check(Options::get_last_request_id()!=='', 'Confirmed request ID stored');
$identity=SecretStore::get_identity();
$responses=array($success);
check((new Registrar())->register(true), 'Registered heartbeat succeeds');
check(end($requests)['payload']['heartbeat']===true, 'Heartbeat retained for known identity');
check(!isset(end($requests)['payload']['site_secret']), 'Heartbeat omits key');

// A complete payload, header and HMAC must use one immutable identity snapshot.
$payload=(new PayloadFactory())->make(false,$identity);
SecretStore::ensure_identity($identity['uuid']);
$responses=array(function($body,$args) use ($identity,$success) {
    $headers=$args['headers'];
    check($body['site_uuid']===$identity['uuid'] && $headers['X-Kosmos-Site-UUID']===$identity['uuid'], 'Payload/header share captured UUID');
    check($body['site_secret']===$identity['secret'], 'Payload uses captured secret');
    $signed=implode('.',array($identity['uuid'],$headers['X-Kosmos-Timestamp'],$headers['X-Kosmos-Nonce'],hash('sha256',$args['body'])));
    check(hash_equals(hash_hmac('sha256',$signed,$identity['secret']),$headers['X-Kosmos-Signature']), 'Signature uses same captured pair');
    return $success($body);
});
check(!is_wp_error((new RegistrationClient())->post($payload,$identity)), 'Snapshot request remains internally consistent');
check(is_wp_error((new RegistrationClient())->post(array('site_uuid'=>'wrong'),$identity)), 'Mixed UUID rejected before HTTP');
$payload['site_secret']='wrong';
check(is_wp_error((new RegistrationClient())->post($payload,$identity)), 'Mixed secret rejected before HTTP');

// A late success from an old identity must never qualify the new pair for heartbeats.
$old=SecretStore::get_identity();
$attempt=RegistrationState::begin($old);
check(is_array($attempt), 'Old request begins');
$domain='https://new-domain.example/';
check(SecretStore::ensure_identity(), 'Concurrent domain transition');
$new=SecretStore::get_identity();
check(!RegistrationState::finish($old,$attempt,'ok','Old success'), 'Old identity reply rejected');
check(!RegistrationState::is_registered(), 'New identity is not registered by old reply');
check(Options::get_last_success_at()==='', 'No misleading stale success displayed');
$responses=array($success);
check((new Registrar())->register(true), 'New identity repairs automatically via full registration');
check(end($requests)['payload']['site_secret']===$new['secret'], 'New identity supplies bootstrap key');

$attempt=RegistrationState::begin($new);
check(is_array($attempt), 'Current attempt starts');
check(RegistrationState::begin($new)===false, 'Concurrent registration is throttled by lease');
$expired=$attempt;
$expired['attempt_expires']=time()-1;
OptionStore::replace(RegistrationState::OPTION,$attempt,$expired);
$newer=RegistrationState::begin($new);
check(is_array($newer), 'Expired registration attempt may be retried');
check(!RegistrationState::finish($new,$attempt,'ok','Late reply'), 'Late same-identity reply cannot overwrite newer attempt');
check(RegistrationState::finish($new,$newer,'ok','New success'), 'Newest attempt can finish');
check(!RegistrationState::finish($new,$newer,'error','Duplicate late error'), 'Duplicate response cannot overwrite confirmed state');

$responses=array(array('code'=>401,'body'=>json_encode(array('detail'=>'Invalid signature'))));
check(!(new Registrar())->register(true), 'Heartbeat failure recorded');
check(Options::get_registration_status()==='error', 'Failure is visible');
check(Options::get_last_success_at()!=='', 'Historical same-identity success remains informational');
check(!RegistrationState::is_registered(), 'Historical success does not suppress retry after error');
check(SecretStore::get_identity()===$new, 'Generic failure never rotates credentials');
$before=count($requests);
Plugin::maybe_retry_registration();
check(count($requests)===$before, 'Retry throttle prevents a request storm');
$state=RegistrationState::current();
$elapsed=$state;
$elapsed['last_attempt_at']=gmdate('c',time()-901);
OptionStore::replace(RegistrationState::OPTION,$state,$elapsed);
$responses=array($success);
Plugin::maybe_retry_registration();
check(count($requests)===$before+1, 'Normal page request retries error despite historical success');
check(end($requests)['payload']['heartbeat']===false && isset(end($requests)['payload']['site_secret']), 'Retry is a full signed registration');
check(RegistrationState::is_registered(), 'Automatic retry recovers');
$before=count($requests);
Plugin::maybe_retry_registration();
check(count($requests)===$before, 'Healthy registration causes no extra network request');

// Hub URL changes must not reuse confirmation from another Hub.
update_option(Options::SERVER_BASE_URL,'https://another-hub.example');
check(!RegistrationState::is_registered(), 'Confirmation scoped to Hub URL');
$responses=array($success);
check((new Registrar())->register(true), 'Changed Hub receives full registration');
check(end($requests)['payload']['heartbeat']===false, 'Changed Hub cannot receive premature heartbeat');
update_option(Options::SERVER_BASE_URL,'http://public.example');
check(!(new Registrar())->register(false), 'Insecure public Hub rejected');
check(SecretStore::get_identity()===$new, 'Transport error does not rotate identity');
delete_option(Options::SERVER_BASE_URL);

$conflict=array('code'=>409,'body'=>json_encode(array('detail'=>array('code'=>'bridge_domain_changed'))));
$responses=array($conflict,$success);
check((new Registrar())->register(true), 'Explicit domain conflict reboots identity once');
$registered=SecretStore::get_identity();
check($registered['uuid']!==$new['uuid'] && $registered['secret']!==$new['secret'], 'Explicit conflict rotates both credentials');
check(end($requests)['payload']['heartbeat']===false && isset(end($requests)['payload']['site_secret']), 'Conflict retry bootstraps');
check(SecretStore::ensure_identity($new['uuid']), 'Late conflict for old UUID is harmless');
check(SecretStore::get_identity()===$registered, 'Late conflict cannot rotate identity twice');
$responses=array($conflict,$conflict);
$before=count($requests);
check(!(new Registrar())->register(), 'Repeated conflict is bounded');
check(count($requests)===$before+2, 'At most one automatic conflict retry');
check(Options::get_registration_status()==='error', 'Repeated conflict remains visible');

foreach (array(array('code'=>200,'body'=>'<html>Login</html>'),array('code'=>200,'body'=>json_encode(array('site_uuid'=>'wrong'))),new WP_Error('timeout','Timeout')) as $response) {
    $responses=array($response);
    check(!(new Registrar())->register(), 'Invalid/failed response is not registration success');
    check(!RegistrationState::is_registered(), 'Invalid response cannot certify identity');
}

reset_fixture();
SecretStore::ensure_identity();
$responses=array(function($payload) use ($success) {
    global $domain;
    $domain='https://during-http.example/';
    SecretStore::ensure_identity();
    return $success($payload);
});
check(!(new Registrar())->register(), 'Domain changes during HTTP: old response rejected end-to-end');
check(!RegistrationState::is_registered(), 'HTTP interleaving cannot mark replacement registered');
$responses=array($success);
check((new Registrar())->register(true), 'Next heartbeat recovers after interleaving');
check(isset(end($requests)['payload']['site_secret']), 'Recovery includes replacement key');

require __DIR__.'/../wordpress-plugin/src/Security/SiteAuth.php';
require __DIR__.'/../wordpress-plugin/src/Http/HubPackageClient.php';
$identity=SecretStore::get_identity();
$timestamp=gmdate('c');
$headers=array('x-kosmos-site-uuid'=>$identity['uuid'],'x-kosmos-timestamp'=>$timestamp,
    'x-kosmos-nonce'=>'offline-test','x-kosmos-body-sha256'=>hash('sha256','{}'));
$headers['x-kosmos-signature']=hash_hmac('sha256',implode('.',array_values($headers)),$identity['secret']);
$incoming=new class($headers) {
    private $headers;
    function __construct($headers) { $this->headers=$headers; }
    function get_header($key) { return $this->headers[$key]; }
    function get_body() { return '{}'; }
};
check(KosmosBridge\Security\SiteAuth::authorize_request($incoming)===true, 'Inbound Hub HMAC still accepted');
check(is_wp_error(KosmosBridge\Security\SiteAuth::authorize_request($incoming)), 'Inbound replay rejected');
$responses=array(function($url,$args) use ($identity) {
    // Emulate a rotation while the download is in progress.
    SecretStore::ensure_identity($identity['uuid']);
    $h=$args['headers'];
    $message=implode('.',array($h['X-Kosmos-Site-UUID'],$h['X-Kosmos-Timestamp'],$h['X-Kosmos-Nonce'],$h['X-Kosmos-Body-SHA256']));
    check($h['X-Kosmos-Site-UUID']===$identity['uuid'], 'Package header uses captured UUID');
    check(hash_equals(hash_hmac('sha256',$message,$identity['secret']),$h['X-Kosmos-Signature']), 'Package signature uses captured matching secret');
    return array('code'=>200,'body'=>'offline-package');
});
check(KosmosBridge\Http\HubPackageClient::download(1)==='offline-package', 'Authenticated package download contract preserved');
check(is_wp_error(KosmosBridge\Security\SiteAuth::authorize_request($incoming)), 'Old identity rejected after rotation');
echo "Bridge registration contracts (real WordPress Options API): $checks passed\n";
