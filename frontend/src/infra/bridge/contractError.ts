import type { CommandName } from '../../app/ports';

export class ContractMismatch extends Error {
  readonly code = 'CONTRACT_MISMATCH';
  constructor(readonly command: CommandName) {
    super('响应与契约不一致：' + command);
    this.name = 'ContractMismatch';
  }
}
