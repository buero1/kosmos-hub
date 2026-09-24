<?php
namespace KosmosBridge\Admin;

use KosmosBridge\Options;
use KosmosBridge\Registration\OptionStore;
use KosmosBridge\Registration\SecretStore;

defined( 'ABSPATH' ) || exit;

class StatusPage {
	/**
	 * @return void
	 */
	public static function register() {
		add_management_page(
			__( 'Kosmos Bridge', 'kosmos-bridge' ),
			__( 'Kosmos Bridge', 'kosmos-bridge' ),
			'manage_options',
			'kosmos-bridge',
			array( self::class, 'render' )
		);
	}

	/**
	 * @return void
	 */
	public static function render() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}

		$identity        = SecretStore::get_identity();
		$site_uuid       = isset( $identity['uuid'] ) ? $identity['uuid'] : '';
		$has_site_secret = ! empty( $identity['secret'] );
		$server_base_url = Options::get_server_base_url();
		$saved_notice    = isset( $_GET['saved'] ) ? sanitize_text_field( wp_unslash( $_GET['saved'] ) ) : '';
		$retry_notice    = isset( $_GET['retried'] ) ? sanitize_text_field( wp_unslash( $_GET['retried'] ) ) : '';
		try {
			$identity_events = OptionStore::read( SecretStore::IDENTITY_EVENTS );
		} catch ( \RuntimeException $error ) {
			$identity_events = array();
		}
		$identity_events = is_array( $identity_events ) ? array_reverse( array_slice( $identity_events, -10 ) ) : array();
		?>
		<div class="wrap">
			<h1><?php echo esc_html__( 'Kosmos Bridge', 'kosmos-bridge' ); ?></h1>
			<p><?php echo esc_html__( 'Current local onboarding state for this site.', 'kosmos-bridge' ); ?></p>

			<?php if ( '1' === $saved_notice ) : ?>
				<div class="notice notice-success"><p><?php echo esc_html__( 'Hub URL saved.', 'kosmos-bridge' ); ?></p></div>
			<?php endif; ?>
			<?php if ( '1' === $retry_notice ) : ?>
				<div class="notice notice-success"><p><?php echo esc_html__( 'Registration retry triggered.', 'kosmos-bridge' ); ?></p></div>
			<?php endif; ?>

			<table class="widefat striped" style="max-width: 880px;">
				<tbody>
					<tr>
						<th><?php echo esc_html__( 'Bridge version', 'kosmos-bridge' ); ?></th>
						<td><?php echo esc_html( Options::get_bridge_version() ); ?></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Identity domain', 'kosmos-bridge' ); ?></th>
						<td><code><?php echo esc_html( \KosmosBridge\Registration\SecretStore::current_domain() ); ?></code></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Site UUID', 'kosmos-bridge' ); ?></th>
						<td><code><?php echo esc_html( $site_uuid ); ?></code></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Site secret', 'kosmos-bridge' ); ?></th>
						<td><?php echo esc_html( $has_site_secret ? 'stored' : 'missing' ); ?></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Hub URL', 'kosmos-bridge' ); ?></th>
						<td><code><?php echo esc_html( $server_base_url ? $server_base_url : 'not configured' ); ?></code></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Default Hub URL', 'kosmos-bridge' ); ?></th>
						<td><code><?php echo esc_html( Options::DEFAULT_SERVER_BASE_URL ); ?></code></td>
						</tr>
					<tr>
						<th><?php echo esc_html__( 'Status', 'kosmos-bridge' ); ?></th>
						<td><?php echo esc_html( Options::get_registration_status() ); ?></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Message', 'kosmos-bridge' ); ?></th>
						<td><?php echo esc_html( Options::get_registration_message() ); ?></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Last attempt', 'kosmos-bridge' ); ?></th>
						<td><?php echo esc_html( Options::get_last_registered_at() ); ?></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Last success', 'kosmos-bridge' ); ?></th>
						<td><?php echo esc_html( Options::get_last_success_at() ); ?></td>
					</tr>
					<tr>
						<th><?php echo esc_html__( 'Last request id', 'kosmos-bridge' ); ?></th>
						<td><code><?php echo esc_html( Options::get_last_request_id() ); ?></code></td>
					</tr>
				</tbody>
			</table>

			<p><?php echo esc_html__( 'Registration status applies only to the current identity and Hub URL. A previous success does not suppress retries after an error.', 'kosmos-bridge' ); ?></p>
			<?php if ( $identity_events ) : ?>
				<details style="max-width: 880px; margin-top: 16px;">
					<summary><?php echo esc_html__( 'Recent identity changes (no secrets)', 'kosmos-bridge' ); ?></summary>
					<table class="widefat striped">
						<thead><tr>
							<th><?php echo esc_html__( 'Time (UTC)', 'kosmos-bridge' ); ?></th>
							<th><?php echo esc_html__( 'Reason', 'kosmos-bridge' ); ?></th>
							<th><?php echo esc_html__( 'Domain', 'kosmos-bridge' ); ?></th>
							<th><?php echo esc_html__( 'Site UUID', 'kosmos-bridge' ); ?></th>
						</tr></thead>
						<tbody><?php foreach ( $identity_events as $event ) : ?>
							<?php if ( ! is_array( $event ) ) { continue; } ?>
							<tr><?php foreach ( array( 'at', 'reason', 'domain', 'uuid' ) as $key ) : ?>
								<td><?php echo esc_html( isset( $event[ $key ] ) && is_string( $event[ $key ] ) ? $event[ $key ] : '' ); ?></td>
							<?php endforeach; ?></tr>
						<?php endforeach; ?></tbody>
					</table>
				</details>
			<?php endif; ?>

			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="max-width: 880px; margin-top: 24px;">
				<input type="hidden" name="action" value="kosmos_bridge_save_settings">
				<?php wp_nonce_field( 'kosmos_bridge_save_settings' ); ?>
				<table class="form-table" role="presentation">
					<tbody>
						<tr>
							<th scope="row">
								<label for="kosmos_hub_base_url"><?php echo esc_html__( 'Kosmos Hub URL', 'kosmos-bridge' ); ?></label>
							</th>
							<td>
								<input
									type="url"
									class="regular-text code"
									id="kosmos_hub_base_url"
									name="kosmos_hub_base_url"
									value="<?php echo esc_attr( $server_base_url ); ?>"
									placeholder="https://hub.example.com"
								>
								<p class="description">
									<?php echo esc_html__( 'Optional override for tests or alternate environments. In normal rollout the default hub URL is used automatically.', 'kosmos-bridge' ); ?>
								</p>
							</td>
						</tr>
					</tbody>
				</table>
				<p>
					<button type="submit" class="button"><?php echo esc_html__( 'Save settings', 'kosmos-bridge' ); ?></button>
				</p>
			</form>

			<p style="margin-top: 16px;">
				<a class="button button-primary" href="<?php echo esc_url( wp_nonce_url( admin_url( 'admin-post.php?action=kosmos_bridge_retry_registration' ), 'kosmos_bridge_retry_registration' ) ); ?>">
					<?php echo esc_html__( 'Retry registration now', 'kosmos-bridge' ); ?>
				</a>
			</p>
		</div>
		<?php
	}
}
